import logging
import os

import psycopg

logger = logging.getLogger(__name__)

_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://sentinel:sentinel@localhost:5432/sentinel",
)


def register_model(run_id: str, model_path: str, threshold: float = 0.5) -> None:
    """Insert a staging entry into model_registry.

    Status is always 'staging' here — promotion to 'active' happens via
    Airflow's retrain DAG after the evaluation pipeline passes.

    ON CONFLICT DO NOTHING makes this safe to re-run if the pipeline retries
    after a partial failure.
    """
    with psycopg.connect(_DSN) as conn:
        conn.execute(
            """
            INSERT INTO model_registry (model_version, model_path, threshold, status)
            VALUES (%s, %s, %s, 'staging')
            ON CONFLICT (model_version) DO NOTHING
            """,
            (run_id, model_path, threshold),
        )
    logger.info(
        "Model registered | version=%s | status=staging | path=%s",
        run_id,
        model_path,
    )
