"""Persist classification results to PostgreSQL and MongoDB."""

import logging
import random
from datetime import datetime, timezone

import psycopg
import pymongo

logger = logging.getLogger(__name__)


def write_classifications(
    conn: psycopg.Connection,
    spans: list[dict],
    results: list[dict],
    model_version: str,
    latency_ms: float,
) -> None:
    """Write one row per span to classifications with at-most-once semantics.

    ON CONFLICT ... DO NOTHING on the partial unique index (span_id, text_type)
    prevents duplicate rows when Kafka redelivers a message.
    """
    now = datetime.now(timezone.utc)
    records = [
        (
            span["text"],
            result["label"],
            result["score"],
            model_version,
            latency_ms,
            now,
            span["span_id"] or None,
            span["text_type"],
        )
        for span, result in zip(spans, results)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO classifications
                (input_text, label, score, model_version, latency_ms, inference_at, span_id, text_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING
            """,
            records,
        )
    conn.commit()
    logger.info("Wrote %d rows to classifications", len(records))


def write_flagged_content(
    db: pymongo.database.Database,
    spans: list[dict],
    results: list[dict],
    model_version: str,
    safe_sample_rate: float,
) -> None:
    """Write harm spans (+ safe_sample_rate fraction of safe) to flagged_content.

    Storing a sample of safe content alongside harm prevents class imbalance
    in the retraining dataset — the retrain pipeline draws from this collection.
    """
    now = datetime.now(timezone.utc)
    docs = []
    for span, result in zip(spans, results):
        label = result["label"]
        if label == "harm" or random.random() < safe_sample_rate:
            docs.append(
                {
                    "ts": now,
                    "input_text": span["text"],
                    "text_type": span["text_type"],
                    "label": label,
                    "score": result["score"],
                    "model_version": model_version,
                    "session_id": span["session_id"],
                    "span_id": span["span_id"],
                    "trace_id": span["trace_id"],
                    "llm_model": span["llm_model"],
                }
            )
    if docs:
        db.flagged_content.insert_many(docs)
        logger.info("Wrote %d documents to flagged_content", len(docs))
