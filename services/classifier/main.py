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
from fastapi import FastAPI, Header, HTTPException
from metrics import BATCH_SIZE, REQUEST_COUNT, REQUEST_LATENCY, attach_log_handler
from model import Classifier
from prometheus_client import make_asgi_app
from schemas import (
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
    active: dict | None = None

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
    #
    # Register the ORIGINAL MinIO-backed path (active["model_path"]) when we
    # resolved via a successful MinIO download — NOT _classifier.model_path.
    # download_model() always caches the downloaded model under a local
    # directory (/tmp/sentinel-model-cache/...), so _classifier.model_path
    # is *always* an absolute local path, even for a MinIO-backed model.
    # Checking model_path.startswith("/") here (an earlier version of this
    # check) therefore skipped registration for the common case too, not
    # just genuine local-only fallback — every classification write then
    # violated classifications_model_version_fkey, since the model_version
    # never existed in model_registry. Reproduced live: confirmed via
    # kubectl logs that a classifier pod loading a real MinIO-backed model
    # still hit "Skipping registry write" and every subsequent /v1/moderations
    # persist attempt failed with psycopg.errors.ForeignKeyViolation.
    #
    # `model_dir is not None` is the correct portability signal: it's only
    # None when Classifier() fell through to _resolve_model_dir() itself
    # (MODEL_PATH env var or logs/optimizer/ found locally) — that's the
    # genuine non-portable case docs/local-dev.md and this comment used to
    # describe, and it's the only one that should skip registration.
    if _pool and model_dir is not None and active is not None:
        try:
            await _db.register_model(
                _pool,
                _classifier.model_version,
                active["model_path"],
                _classifier.threshold,
            )
        except Exception:
            logger.exception("Failed to register model version in registry")
    elif _pool:
        logger.info(
            "Skipping registry write — resolved via local fallback, "
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
    """Used by moderate() for list input: run inference off the event loop
    via a single direct run_in_executor dispatch (the caller already batched
    its own texts), record per-endpoint metrics, and fire off persistence.
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


async def _moderate_single(text: str, persist: bool) -> dict:
    """Single-string input path: goes through DynamicBatcher so concurrent
    single-item /v1/moderations calls still get coalesced into one ORT call
    per batch, instead of one run_in_executor dispatch per request.
    """
    t0 = time.perf_counter()
    try:
        result = await _batcher.submit(text)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="classifier queue full — retry later")
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    inference_at = datetime.now(timezone.utc)

    REQUEST_COUNT.labels(endpoint="moderations", label=result["label"]).inc()
    REQUEST_LATENCY.labels(endpoint="moderations").observe(latency_ms / 1000)

    if _pool and persist:
        task = asyncio.create_task(
            _persist_single(
                text,
                result["label"],
                result["score"],
                _classifier.model_version,
                latency_ms,
                inference_at,
            )
        )
        _persist_tasks.add(task)
        task.add_done_callback(_persist_tasks.discard)

    return result


@app.post("/v1/moderations", response_model=ModerationResponse)
async def moderate(
    request: ModerationRequest,
    x_sentinel_skip_persist: bool = Header(False, alias="X-Sentinel-Skip-Persist"),
) -> ModerationResponse:
    """OpenAI Moderation API-compatible endpoint.

    Accepts a single string or a list of strings and returns one ModerationResult
    per input in the same order. Drop-in compatible with openai.moderations.create().

    This is the only classifier endpoint — the stream processor calls it too,
    so internal traffic exercises the same code path external callers use.
    A single-string input is queued through DynamicBatcher (coalesces
    concurrent single-item calls into one ORT call per batch); a list input
    is already batched by the caller, so it's dispatched directly via
    run_in_executor. Either way session.run() never runs inline on the
    event loop. Persistence is skipped via the X-Sentinel-Skip-Persist
    header (the stream processor writes to PG itself, with span_id for
    idempotency) rather than a body field — ModerationRequest stays a clean
    OpenAI-compatible schema with no Sentinel-internal fields visible to
    external callers.
    """
    persist = not x_sentinel_skip_persist

    if isinstance(request.input, str):
        results = [await _moderate_single(request.input, persist)]
    else:
        results, _, _ = await _classify_and_persist(
            list(request.input), "moderations", persist=persist
        )

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
