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
    logger.info(
        "Reference scores loaded | model=%s | n=%d", model_version, len(scores)
    )
    return scores


def read_current_scores(
    database_url: str,
    model_version: str,
    hours: int = 24,
) -> tuple[list[float], datetime, datetime]:
    """Return scores from the last `hours` for this model version.

    Also returns (window_start, window_end) as UTC datetimes for drift_stats.
    """
    with _connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT score, ts FROM classifications
            WHERE model_version = %s
              AND ts >= NOW() - make_interval(hours => %s)
            ORDER BY ts ASC
            """,
            (model_version, hours),
        ).fetchall()

    if not rows:
        return [], datetime.now(timezone.utc), datetime.now(timezone.utc)

    scores = [r[0] for r in rows]
    window_start = rows[0][1]
    window_end = rows[-1][1]

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
    """Return the model_version model_registry considers current.

    Mirrors services/classifier/db.py's get_active_model selection exactly
    (prefer status='active', fall back to the most recent 'staging' entry) so
    the drift job evaluates the same version a classifier pod would load on
    startup. Reading from `classifications` instead (whichever version wrote
    the most recent row) was a race during rolling restarts: old- and
    new-version pods write concurrently, so "most recent row" doesn't mean
    "the rollout's target version" — it could attribute drift_stats to a
    version that's already being retired.
    """
    with _connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT model_version FROM model_registry
            WHERE status IN ('active', 'staging')
            ORDER BY
                CASE status WHEN 'active' THEN 0 ELSE 1 END,
                COALESCE(promoted_at, created_at) DESC
            LIMIT 1
            """,
        ).fetchone()
    return row[0] if row else None
