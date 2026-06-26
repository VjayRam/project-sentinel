import logging
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)


async def init_pool(dsn: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=5)
    logger.info("DB pool created | dsn=%s", dsn.split("@")[-1])
    return pool


async def close_pool(pool: asyncpg.Pool) -> None:
    await pool.close()
    logger.info("DB pool closed")


async def register_model(
    pool: asyncpg.Pool,
    model_version: str,
    model_path: str,
    threshold: float,
) -> None:
    """Insert this model version into model_registry if it isn't already there.

    ON CONFLICT DO NOTHING makes this safe to call on every pod startup —
    the first pod registers the version; subsequent pods skip it silently.
    """
    await pool.execute(
        """
        INSERT INTO model_registry (model_version, model_path, threshold, status)
        VALUES ($1, $2, $3, 'active')
        ON CONFLICT (model_version) DO NOTHING
        """,
        model_version,
        model_path,
        threshold,
    )
    logger.info("Model registered | version=%s", model_version)


async def write_classification(
    pool: asyncpg.Pool,
    input_text: str,
    label: str,
    score: float,
    model_version: str,
    latency_ms: float,
    inference_at: datetime,
) -> None:
    await pool.execute(
        """
        INSERT INTO classifications (input_text, label, score, model_version, latency_ms, inference_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        input_text,
        label,
        score,
        model_version,
        latency_ms,
        inference_at,
    )


async def write_classifications_batch(
    pool: asyncpg.Pool,
    records: list[tuple],
) -> None:
    """Insert multiple classifications in a single round-trip.

    Each record is (input_text, label, score, model_version, latency_ms, inference_at).
    executemany sends all rows in one network call.
    """
    await pool.executemany(
        """
        INSERT INTO classifications (input_text, label, score, model_version, latency_ms, inference_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        records,
    )
