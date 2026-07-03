"""
PostgreSQL helpers for the drift job.

read_reference_scores  — earliest N rows per model_version (training baseline)
read_current_scores    — last `hours` of rows per model_version
write_drift_stats      — insert one row into drift_stats
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import psycopg

logger = logging.getLogger(__name__)

# Number of earliest classifications used as the reference distribution.
# Represents "what the model saw at deploy time."
REFERENCE_SIZE = 1000


def _connect(database_url: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url)
    logger.info("PostgreSQL connected")
    return conn


def read_reference_scores(
    database_url: str,
    model_version: str,
    size: int = REFERENCE_SIZE,
) -> list[float]:
    """Return scores from the earliest `size` rows for this model version.

    These form the reference distribution — what the score distribution looked
    like when the model was first deployed.
    """
    with _connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT score FROM classifications
            WHERE model_version = %s
            ORDER BY ts ASC
            LIMIT %s
            """,
            (model_version, size),
        ).fetchall()

    scores = [r[0] for r in rows]
    logger.info("Reference scores loaded | model=%s | n=%d", model_version, len(scores))
    return scores


#  Row cap for the current window — bounds how much data gets pulled into
#  the driver process's memory (via psycopg here, then again via Spark's
#  createDataFrame([...])) before Spark ever runs. Generous relative to a
#  typical drift window: even at this cap, `scores` is a few MB of floats.
MAX_CURRENT_ROWS = 100_000


def read_current_scores(
    database_url: str,
    model_version: str,
    hours: int = 24,
    max_rows: int = MAX_CURRENT_ROWS,
) -> tuple[list[float], datetime, datetime]:
    """Return scores from the last `hours` for this model version, capped at
    `max_rows` (the most recent rows in the window, not the oldest).

    Also returns (window_start, window_end) as UTC datetimes for drift_stats.
    """
    with _connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT score, ts FROM (
                SELECT score, ts FROM classifications
                WHERE model_version = %s
                  AND ts >= NOW() - make_interval(hours => %s)
                ORDER BY ts DESC
                LIMIT %s
            ) recent
            ORDER BY ts ASC
            """,
            (model_version, hours, max_rows),
        ).fetchall()

    if not rows:
        return [], datetime.now(timezone.utc), datetime.now(timezone.utc)

    scores = [r[0] for r in rows]
    window_start = rows[0][1]
    window_end = rows[-1][1]

    if len(scores) == max_rows:
        logger.warning(
            "Current scores hit max_rows cap (%d) — window may include more "
            "than %dh of data; drift stats reflect the most recent %d rows only",
            max_rows,
            hours,
            max_rows,
        )
    logger.info(
        "Current scores loaded | model=%s | n=%d | window=%s → %s",
        model_version,
        len(scores),
        window_start.isoformat(),
        window_end.isoformat(),
    )
    return scores, window_start, window_end


def write_drift_stats(
    database_url: str,
    *,
    model_version: str,
    window_start: datetime,
    window_end: datetime,
    n_samples: int,
    psi: float,
    jsd: float,
    drift_flagged: bool,
) -> None:
    with _connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO drift_stats
                (model_version, window_start, window_end, n_samples, psi, jsd, drift_flagged)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (model_version, window_start, window_end, n_samples, psi, jsd, drift_flagged),
        )

    logger.info(
        "drift_stats written | model=%s | PSI=%.4f | JSD=%.4f | flagged=%s",
        model_version,
        psi,
        jsd,
        drift_flagged,
    )


def get_active_model_version(database_url: str) -> str | None:
    """Return the model_version that's actually appearing in classifications.

    Live-verified gotcha this has to account for: model_registry holds two
    different kinds of rows that don't share a model_version namespace.
    services/classifier/db.py's get_active_model() picks a 'active'/'staging'
    row to decide which MinIO model_path to download — but once loaded, the
    classifier self-registers a *separate*, new 'staging' row under its own
    freshly-derived model_version string (services/classifier/model.py's
    `sentinel-roberta-{deployed_at}-{quant_tag}`), and THAT string — not the
    row it downloaded from — is what ends up in classifications.model_version
    on every write. An earlier version of this function mirrored
    get_active_model()'s "prefer status='active'" ordering exactly, which
    seemed principled but was wrong in practice: reproduced live against a
    real cluster, the 'active' row was a stale promotion from a different
    model_version string entirely (nothing in this project's current phase —
    Airflow/Phase 7 doesn't exist yet — reliably keeps 'active' pointed at
    what's actually running), so it returned a model_version with zero
    matching classification rows and drift detection silently found nothing.
    Ordering by created_at alone — "whichever pod self-registered most
    recently" — is what's actually running and writing classifications.
    Still scoped to non-retired rows; still subject to the same rolling-
    restart race noted before (old- and new-version pods can both register
    around the same moment), just no longer compounded by a status
    preference that points at the wrong namespace entirely.
    """
    with _connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT model_version FROM model_registry
            WHERE status IN ('active', 'staging')
            ORDER BY created_at DESC
            LIMIT 1
            """,
        ).fetchone()
    return row[0] if row else None
