import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import db as _db
from batcher import DynamicBatcher
from config import settings
from download import download_model
from fastapi import FastAPI, HTTPException
from metrics import BATCH_SIZE, REQUEST_COUNT, REQUEST_LATENCY, attach_log_handler
from model import Classifier
from prometheus_client import make_asgi_app
from schemas import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    ClassifyRequest,
    ClassifyResponse,
    ClassifyResult,
    ModerationCategories,
    ModerationCategoryScores,
    ModerationRequest,
    ModerationResponse,
    ModerationResult,
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
_ready: bool = False
_persist_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _classifier, _batcher, _pool, _ready

    # ── 1. Connect to DB and resolve the active model from the registry ────────
    # The registry is the source of truth for which model version to serve.
    # Open the pool first so we can query it before loading the model.
    model_dir: Path | None = None

    if settings.database_url:
        try:
            _pool = await _db.init_pool(settings.database_url)
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
            if _pool is not None:
                # init_pool succeeded (opens a connection eagerly, min_size=1)
                # but a later call in this block failed — close it before
                # dropping the reference, or that connection leaks for the
                # lifetime of the pod.
                await _pool.close()
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
    # Skip registration when model_path is an absolute local path (this pod
    # fell back to local model discovery — MODEL_PATH env var or
    # logs/optimizer/). That path only exists on THIS pod's filesystem;
    # download.py treats any path starting with "/" as already-materialized
    # and never fetches it from MinIO, so writing it to the shared registry
    # would hand a different pod a path that doesn't exist on its own
    # filesystem, crashing it on startup. Leaving the registry untouched
    # here means other pods fall through to their own local discovery too,
    # which is at least self-consistent per pod.
    if _pool and not _classifier.model_path.startswith("/"):
        try:
            await _db.register_model(
                _pool,
                _classifier.model_version,
                _classifier.model_path,
                _classifier.threshold,
            )
        except Exception:
            logger.exception("Failed to register model version in registry")
    elif _pool:
        logger.info(
            "Skipping registry write — model_path is a local filesystem path, "
            "not portable across pods | path=%s",
            _classifier.model_path,
        )

    # ── 4. Signal readiness — pod now accepts traffic ─────────────────────────
    _ready = True

    yield

    # ── Shutdown: stop accepting traffic before tearing down ──────────────────
    _ready = False
    await _batcher.stop()
    if _pool:
        # Drain any in-flight fire-and-forget persist tasks before closing the
        # pool — without this, classifications written during the last few
        # requests are silently dropped when the event loop stops.
        if _persist_tasks:
            await asyncio.gather(*_persist_tasks, return_exceptions=True)
        await _db.close_pool(_pool)
    _classifier = None


app = FastAPI(title="Sentinel Classifier", version="1.0.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/health/live")
def liveness() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict:
    if not _ready:
        raise HTTPException(status_code=503, detail="not ready")
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


async def _classify_and_persist(
    texts: list[str], endpoint: str, persist: bool
) -> tuple[list[dict], float, datetime]:
    """Shared by classify_batch() and moderate(): run inference off the event
    loop, record per-endpoint metrics, and fire off persistence. Callers just
    shape their own response from the (results, latency_ms, inference_at).
    """
    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _classifier.predict, texts)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    inference_at = datetime.now(timezone.utc)

    BATCH_SIZE.observe(len(texts))
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(latency_ms / 1000)
    for r in results:
        REQUEST_COUNT.labels(endpoint=endpoint, label=r["label"]).inc()

    if _pool and persist:
        records = [
            (text, r["label"], r["score"], _classifier.model_version, latency_ms, inference_at)
            for text, r in zip(texts, results)
        ]
        task = asyncio.create_task(_persist_batch(records))
        _persist_tasks.add(task)
        task.add_done_callback(_persist_tasks.discard)

    return results, latency_ms, inference_at


@app.post("/classify", response_model=ClassifyResponse)
async def classify(request: ClassifyRequest) -> ClassifyResponse:
    t0 = time.perf_counter()
    try:
        result = await _batcher.submit(request.text)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="classifier queue full — retry later")
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    inference_at = datetime.now(timezone.utc)

    REQUEST_COUNT.labels(endpoint="classify", label=result["label"]).inc()
    REQUEST_LATENCY.labels(endpoint="classify").observe(latency_ms / 1000)

    if _pool:
        task = asyncio.create_task(
            _persist_single(
                request.text,
                result["label"],
                result["score"],
                _classifier.model_version,
                latency_ms,
                inference_at,
            )
        )
        _persist_tasks.add(task)
        task.add_done_callback(_persist_tasks.discard)

    return ClassifyResponse(
        latency_ms=latency_ms,
        model_version=_classifier.model_version,
        inference_at=inference_at.isoformat(),
        **result,
    )


@app.post("/classify/batch", response_model=BatchClassifyResponse)
async def classify_batch(request: BatchClassifyRequest) -> BatchClassifyResponse:
    results, latency_ms, inference_at = await _classify_and_persist(
        request.texts, "classify_batch", request.persist
    )
    return BatchClassifyResponse(
        results=[ClassifyResult(**r) for r in results],
        latency_ms=latency_ms,
        batch_size=len(request.texts),
        model_version=_classifier.model_version,
        inference_at=inference_at.isoformat(),
    )


@app.post("/v1/moderations", response_model=ModerationResponse)
async def moderate(request: ModerationRequest) -> ModerationResponse:
    """OpenAI Moderation API-compatible endpoint.

    Accepts a single string or a list of strings and returns one ModerationResult
    per input in the same order. Drop-in compatible with openai.moderations.create().
    """
    texts = [request.input] if isinstance(request.input, str) else list(request.input)
    # Always persists — this is the public OpenAI-compatible surface, so it
    # doesn't carry a Sentinel-internal skip-persistence knob (see schemas.py:
    # ModerationRequest used to have a `persist` field for exactly that; the
    # stream processor now calls /classify/batch instead, which still has one
    # since that endpoint is explicitly internal/testing-only).
    results, _, _ = await _classify_and_persist(texts, "moderations", persist=True)

    return ModerationResponse(
        id=f"modr-{uuid.uuid4().hex[:12]}",
        model=_classifier.model_version,
        results=[
            ModerationResult(
                flagged=r["label"] == "harm",
                categories=ModerationCategories(harm=r["label"] == "harm"),
                category_scores=ModerationCategoryScores(harm=round(r["score"], 6)),
            )
            for r in results
        ],
    )
