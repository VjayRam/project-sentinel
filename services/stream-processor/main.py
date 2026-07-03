"""Sentinel stream processor — Kafka → classify → PostgreSQL + MongoDB.

Consuming loop:
  1. Poll traces.raw for a batch of OTLP JSON messages.
  2. Parse each message to extract LLM spans (prompt + response text).
  3. POST /v1/moderations (X-Sentinel-Skip-Persist: true) — the same
     OpenAI-compatible endpoint external callers use, so our own highest-
     volume traffic exercises it too. Classifier skips its own PG write;
     we handle PG writes here with span_id for idempotency.
  4. Write all results to PostgreSQL classifications.
  5. Write harm + sampled safe to MongoDB flagged_content.
  6. Commit Kafka offset ONLY after both writes succeed.
     If classify or DB fails, do not commit — Kafka redelivers the batch.
"""

import json
import logging
import os
import signal
import time

import httpx
import psycopg
import pymongo
from kafka import KafkaConsumer
from processor import extract_spans
from writer import write_classifications, write_flagged_content

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")
TOPIC = "traces.raw"
GROUP_ID = "sentinel-stream-processor"
CLASSIFIER_URL = os.environ.get("CLASSIFIER_URL", "http://localhost:8000")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel"
)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://sentinel:sentinel@localhost:27017/sentinel")
SAFE_SAMPLE_RATE = float(os.environ.get("SAFE_SAMPLE_RATE", "0.1"))
MAX_POLL_RECORDS = int(os.environ.get("MAX_POLL_RECORDS", "50"))
# Must not exceed the classifier's MAX_BATCH_SIZE (services/classifier/config.py's
# settings.max_batch_size, env var MAX_BATCH_SIZE — same default) or
# POST /v1/moderations 422s on oversized chunks. Not read from the same env
# var name since this is a separately deployed service — set both explicitly
# if either is tuned away from the shared default.
CLASSIFY_CHUNK_SIZE = int(os.environ.get("CLASSIFY_CHUNK_SIZE", "64"))

_running = True


def _handle_signal(sig, frame):
    global _running
    logger.info("Received signal %d — shutting down", sig)
    _running = False


def _moderation_results_to_label_score(results: list[dict]) -> list[dict]:
    """Translate /v1/moderations' OpenAI-compatible result shape into this
    service's internal {label, score} shape (what write_classifications and
    write_flagged_content expect). Named explicitly rather than an inline
    remap so the translation is a visible, intentional boundary — calling
    the OpenAI-compatible endpoint from internal code means this mapping is
    inherent, not something to hide.
    """
    return [
        {"label": "harm" if r["flagged"] else "safe", "score": r["category_scores"]["harm"]}
        for r in results
    ]


def _pg_connect() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL)
    logger.info("PostgreSQL connected")
    return conn


def _pg_write(
    pg_conn: psycopg.Connection,
    spans: list[dict],
    results: list[dict],
    model_version: str,
    latency_ms: float,
    mongo_db,
) -> psycopg.Connection:
    """Write to PostgreSQL and MongoDB, reconnecting once on connection failure.

    Returns the (possibly new) connection so the caller can update its reference.
    Raises on failure so the caller knows not to commit the Kafka offset — the
    connection passed in (or opened during the retry) is guaranteed closed in
    that case, so the caller never holds a reference to a leaked/half-open
    connection: the next call always reconnects from scratch instead of
    reusing something nobody closed.
    ON CONFLICT DO NOTHING on (span_id, text_type) makes the retry idempotent.
    """
    try:
        write_classifications(pg_conn, spans, results, model_version, latency_ms)
        write_flagged_content(mongo_db, spans, results, model_version, SAFE_SAMPLE_RATE)
        return pg_conn
    except psycopg.OperationalError:
        logger.warning("PostgreSQL connection lost — reconnecting and retrying once")
        try:
            pg_conn.close()
        except Exception:
            pass

        pg_conn = _pg_connect()
        try:
            write_classifications(pg_conn, spans, results, model_version, latency_ms)
            write_flagged_content(mongo_db, spans, results, model_version, SAFE_SAMPLE_RATE)
            return pg_conn
        except Exception:
            # Retry also failed (e.g. PG still down) — close this connection
            # too instead of leaking it silently on every double-failure.
            try:
                pg_conn.close()
            except Exception:
                pass
            raise


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    pg_conn = _pg_connect()
    mongo_db = pymongo.MongoClient(MONGO_URI).sentinel

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        request_timeout_ms=40_000,
        session_timeout_ms=30_000,
        heartbeat_interval_ms=10_000,
    )

    logger.info(
        "Stream processor started | bootstrap=%s topic=%s group=%s",
        KAFKA_BOOTSTRAP,
        TOPIC,
        GROUP_ID,
    )

    with httpx.Client(base_url=CLASSIFIER_URL, timeout=60.0) as http:
        while _running:
            records = consumer.poll(timeout_ms=1000, max_records=MAX_POLL_RECORDS)
            if not records:
                continue

            spans: list[dict] = []
            for tp, messages in records.items():
                for msg in messages:
                    try:
                        spans.extend(extract_spans(msg.value))
                    except Exception:
                        logger.exception("Malformed OTLP message offset=%d — skipping", msg.offset)

            if not spans:
                consumer.commit()
                continue

            texts = [s["text"] for s in spans]

            # Chunk texts to stay within the classifier's MAX_BATCH_SIZE per
            # request. A single Kafka poll can accumulate many spans across
            # multiple messages.
            chunks = [
                texts[i : i + CLASSIFY_CHUNK_SIZE]
                for i in range(0, len(texts), CLASSIFY_CHUNK_SIZE)
            ]

            # Calls /v1/moderations (not /classify/batch) deliberately — this
            # is the same OpenAI-compatible endpoint external callers use, so
            # our own highest-volume internal traffic exercises that exact
            # code path (see CLAUDE.md's OTel GenAI conventions section).
            # X-Sentinel-Skip-Persist replaces the classifier's own PG write:
            # we write here ourselves with span_id for idempotency.
            all_results: list[dict] = []
            classify_ms_total = 0.0
            model_version = ""
            try:
                for chunk in chunks:
                    t0 = time.perf_counter()
                    resp = http.post(
                        "/v1/moderations",
                        json={"input": chunk},
                        headers={"X-Sentinel-Skip-Persist": "true"},
                    )
                    if not resp.is_success:
                        logger.error(
                            "Classify HTTP %d — body: %s | chunk=%d texts",
                            resp.status_code,
                            resp.text[:300],
                            len(chunk),
                        )
                    resp.raise_for_status()
                    classify_ms_total += (time.perf_counter() - t0) * 1000
                    body = resp.json()
                    model_version = body["model"]
                    all_results.extend(_moderation_results_to_label_score(body["results"]))
            except Exception:
                logger.exception("Classify failed — not committing, Kafka will redeliver")
                continue

            per_span_latency_ms = classify_ms_total / max(len(texts), 1)

            try:
                pg_conn = _pg_write(
                    pg_conn, spans, all_results, model_version, per_span_latency_ms, mongo_db
                )
            except Exception:
                logger.exception("DB write failed — not committing, Kafka will redeliver")
                continue

            consumer.commit()
            logger.info("Committed offset after processing %d spans", len(spans))

    consumer.close()
    if not pg_conn.closed:
        pg_conn.close()
    logger.info("Stream processor stopped")


if __name__ == "__main__":
    run()
