import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from batcher import DynamicBatcher
from metrics import BATCH_SIZE, REQUEST_COUNT, REQUEST_LATENCY, attach_log_handler
from model import Classifier
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

_classifier: Classifier | None = None
_batcher: DynamicBatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _classifier, _batcher
    _classifier = Classifier()
    _classifier.warmup()
    _batcher = DynamicBatcher(_classifier.predict)
    _batcher.start()
    yield
    _batcher.stop()
    _classifier = None


app = FastAPI(title="Sentinel Classifier", version="1.0.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": _classifier.model_id if _classifier else None}


@app.post("/classify", response_model=ClassifyResponse)
async def classify(request: ClassifyRequest) -> ClassifyResponse:
    t0 = time.perf_counter()
    result = await _batcher.submit(request.text)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    REQUEST_COUNT.labels(endpoint="classify", label=result["label"]).inc()
    REQUEST_LATENCY.labels(endpoint="classify").observe(latency_ms / 1000)

    return ClassifyResponse(
        latency_ms=latency_ms,
        model_version=_classifier.model_version,
        inference_at=datetime.now(timezone.utc).isoformat(),
        **result,
    )


@app.post("/classify/batch", response_model=BatchClassifyResponse)
def classify_batch(request: BatchClassifyRequest) -> BatchClassifyResponse:
    t0 = time.perf_counter()
    results = _classifier.predict(request.texts)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    BATCH_SIZE.observe(len(request.texts))
    REQUEST_LATENCY.labels(endpoint="classify_batch").observe(latency_ms / 1000)
    for r in results:
        REQUEST_COUNT.labels(endpoint="classify_batch", label=r["label"]).inc()

    return BatchClassifyResponse(
        results=[ClassifyResult(**r) for r in results],
        latency_ms=latency_ms,
        batch_size=len(request.texts),
        model_version=_classifier.model_version,
        inference_at=datetime.now(timezone.utc).isoformat(),
    )
