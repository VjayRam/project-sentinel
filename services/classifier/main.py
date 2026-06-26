import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import db as _db
from batcher import DynamicBatcher
from download import download_model
from fastapi import FastAPI
from metrics import BATCH_SIZE, REQUEST_COUNT, REQUEST_LATENCY, attach_log_handler
from model import Classifier
from prometheus_client import make_asgi_app
from schemas import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassifyRequest,
    ClassifyResponse,
    ClassifyResult,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
attach_log_handler()

logger = logging.getLogger(__name__)

_classifier: Classifier | None = None
_batcher: DynamicBatcher | None = None
_pool = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _classifier, _batcher, _pool

    # ── 1. Connect to DB and resolve the active model from the registry ────────
    # The registry is the source of truth for which model version to serve.
    # Open the pool first so we can query it before loading the model.
    model_dir: Path | None = None
    dsn = os.environ.get("DATABASE_URL")

    if dsn:
        try:
            _pool = await _db.init_pool(dsn)
            active = await _db.get_active_model(_pool)
            if active:
                # download_model is blocking boto3 I/O — offload to a thread so
                # the event loop stays responsive during the transfer.
                loop = asyncio.get_running_loop()
                model_dir = await loop.run_in_executor(None, download_model, active["model_path"])
                if model_dir is None:
                    logger.warning(
                        "Could not resolve model from registry | version=%s | path=%s"
                        " — falling back to local model discovery",
                        active["model_version"],
                        active["model_path"],
                    )
            else:
                logger.info("Registry empty — using local model discovery")
        except Exception:
            logger.exception("DB init failed — running without persistence")
            _pool = None
    else:
        logger.warning("DATABASE_URL not set — classifications will not be persisted")

    # ── 2. Load the classifier ─────────────────────────────────────────────────
    # model_dir=None falls through to MODEL_PATH env var, then logs/optimizer/.
    _classifier = Classifier(model_dir=model_dir)
    _classifier.warmup()
    _batcher = DynamicBatcher(_classifier.predict)
    _batcher.start()

    # ── 3. Record this model version in the registry (idempotent) ─────────────
    # ON CONFLICT DO NOTHING: first pod registers it, subsequent pods skip.
    if _pool:
        try:
            await _db.register_model(
                _pool,
                _classifier.model_version,
                _classifier.model_path,
                _classifier.threshold,
            )
        except Exception:
            logger.exception("Failed to register model version in registry")

    yield

    _batcher.stop()
    if _pool:
        await _db.close_pool(_pool)
    _classifier = None


app = FastAPI(title="Sentinel Classifier", version="1.0.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": _classifier.model_id if _classifier else None}


async def _persist_single(
    text: str,
    label: str,
    score: float,
    model_version: str,
    latency_ms: float,
    inference_at: datetime,
) -> None:
    try:
        await _db.write_classification(
            _pool, text, label, score, model_version, latency_ms, inference_at
        )
    except Exception:
        logger.exception("Failed to persist classification")


async def _persist_batch(records: list[tuple]) -> None:
    try:
        await _db.write_classifications_batch(_pool, records)
    except Exception:
        logger.exception("Failed to persist batch classifications")


@app.post("/classify", response_model=ClassifyResponse)
async def classify(request: ClassifyRequest) -> ClassifyResponse:
    t0 = time.perf_counter()
    result = await _batcher.submit(request.text)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    inference_at = datetime.now(timezone.utc)

    REQUEST_COUNT.labels(endpoint="classify", label=result["label"]).inc()
    REQUEST_LATENCY.labels(endpoint="classify").observe(latency_ms / 1000)

    if _pool:
        asyncio.create_task(
            _persist_single(
                request.text,
                result["label"],
                result["score"],
                _classifier.model_version,
                latency_ms,
                inference_at,
            )
        )

    return ClassifyResponse(
        latency_ms=latency_ms,
        model_version=_classifier.model_version,
        inference_at=inference_at.isoformat(),
        **result,
    )


@app.post("/classify/batch", response_model=BatchClassifyResponse)
async def classify_batch(request: BatchClassifyRequest) -> BatchClassifyResponse:
    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _classifier.predict, request.texts)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    inference_at = datetime.now(timezone.utc)

    BATCH_SIZE.observe(len(request.texts))
    REQUEST_LATENCY.labels(endpoint="classify_batch").observe(latency_ms / 1000)
    for r in results:
        REQUEST_COUNT.labels(endpoint="classify_batch", label=r["label"]).inc()

    if _pool:
        records = [
            (text, r["label"], r["score"], _classifier.model_version, latency_ms, inference_at)
            for text, r in zip(request.texts, results)
        ]
        asyncio.create_task(_persist_batch(records))

    return BatchClassifyResponse(
        results=[ClassifyResult(**r) for r in results],
        latency_ms=latency_ms,
        batch_size=len(request.texts),
        model_version=_classifier.model_version,
        inference_at=inference_at.isoformat(),
    )
