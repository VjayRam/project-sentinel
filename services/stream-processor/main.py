"""Sentinel stream processor — Kafka → classify → PostgreSQL + MongoDB.

Consuming loop:
  1. Poll traces.raw for a batch of OTLP JSON messages.
  2. Parse each message to extract LLM spans (prompt + response text).
  3. POST /classify/batch?persist=False — classifier returns labels without
     writing to PG itself (we handle PG writes here with span_id for idempotency).
  4. Write all results to PostgreSQL classifications.
  5. Write harm + sampled safe to MongoDB flagged_content.
  6. Commit Kafka offset ONLY after both writes succeed.
     If classify or DB fails, do not commit — Kafka redelivers the batch.
"""

import json
import logging
import os
import signal
import sys

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
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://sentinel:sentinel@localhost:5432/sentinel")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://sentinel:sentinel@localhost:27017/sentinel")
SAFE_SAMPLE_RATE = float(os.environ.get("SAFE_SAMPLE_RATE", "0.1"))
MAX_POLL_RECORDS = int(os.environ.get("MAX_POLL_RECORDS", "50"))

_running = True


def _handle_signal(sig, frame):
    global _running
    logger.info("Received signal %d — shutting down", sig)
    _running = False


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    pg_conn = psycopg.connect(DATABASE_URL)
    mongo_db = pymongo.MongoClient(MONGO_URI).sentinel

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        request_timeout_ms=30_000,
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

            # persist=False: skip classifier's own PG write; we write here with span_id.
            try:
                resp = http.post("/classify/batch", json={"texts": texts, "persist": False})
                resp.raise_for_status()
                body = resp.json()
            except Exception:
                logger.exception("Classify failed — not committing, Kafka will redeliver")
                continue

            results = body["results"]
            model_version = body["model_version"]
            per_span_latency_ms = body["latency_ms"] / max(len(texts), 1)

            try:
                write_classifications(pg_conn, spans, results, model_version, per_span_latency_ms)
                write_flagged_content(mongo_db, spans, results, model_version, SAFE_SAMPLE_RATE)
            except Exception:
                logger.exception("DB write failed — not committing, Kafka will redeliver")
                continue

            consumer.commit()
            logger.info("Committed offset after processing %d spans", len(spans))

    consumer.close()
    pg_conn.close()
    logger.info("Stream processor stopped")


if __name__ == "__main__":
    run()
