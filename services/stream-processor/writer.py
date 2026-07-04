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
    """Write harm spans (+ safe_sample_rate fraction of safe) to flagged_content
    with at-most-once semantics per (span_id, text_type), mirroring the
    ON CONFLICT DO NOTHING behavior of write_classifications.

    A plain insert_many here would duplicate documents on Kafka redelivery: a
    mid-batch failure (ordered=True stops partway through, or any exception
    after some docs already landed) leaves the offset uncommitted, so the same
    batch gets reprocessed and re-inserted with no dedup. Upserting on
    (span_id, text_type) — the same natural key as the classifications unique
    index — makes redelivery a no-op instead of a duplicate.
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
    if not docs:
        return

    # Spans without a span_id have no natural key to dedupe on (same gap that
    # already exists for classifications' partial unique index) — insert
    # those directly; upsert the rest keyed on (span_id, text_type).
    #
    # manual_label/training_decision (set by services/label-ui) go in
    # $setOnInsert, not $set: a Kafka redelivery of an already-labelled span
    # re-runs this upsert with the same ingestion fields, and $set would
    # silently overwrite a completed manual label back to "pending" every
    # time. $setOnInsert only applies the defaults the first time the
    # document is created, so relabelling data can never be clobbered by
    # redelivery.
    operations = [
        pymongo.UpdateOne(
            {"span_id": doc["span_id"], "text_type": doc["text_type"]},
            {
                "$set": doc,
                "$setOnInsert": {"manual_label": None, "training_decision": "pending"},
            },
            upsert=True,
        )
        if doc["span_id"]
        else pymongo.InsertOne({**doc, "manual_label": None, "training_decision": "pending"})
        for doc in docs
    ]
    # ordered=False: one bad op doesn't block the rest from committing, so a
    # partial failure leaves only genuinely-failed docs to retry — everything
    # else is already a safe upsert and won't duplicate on redelivery.
    result = db.flagged_content.bulk_write(operations, ordered=False)
    logger.info(
        "flagged_content write | upserted=%d modified=%d inserted=%d",
        len(result.upserted_ids),
        result.modified_count,
        result.inserted_count,
    )
