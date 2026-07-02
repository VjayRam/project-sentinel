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
    """Return the model_version string most recently written to classifications.

    The classifier computes its own version string (sentinel-roberta-{ts}-int8)
    at load time and registers it as 'staging'. model_registry's 'active' row
    tracks the MinIO path entry, not the classifier's self-reported version.
    Querying classifications directly gives us the version that is actually
    running and whose rows we want to evaluate for drift.
    """
    with _connect(database_url) as conn:
        row = conn.execute(
            """
            SELECT model_version FROM classifications
            ORDER BY ts DESC
            LIMIT 1
            """,
        ).fetchone()
    return row[0] if row else None
