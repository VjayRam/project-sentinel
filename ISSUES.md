# Production Gaps

Issues found during an early audit of the classifier service and infra stack against real large-scale production patterns — written when only Phases 1–4 existed. Ordered by impact — items near the top would block a real deployment; items near the bottom are best practices that catch up with you over time.

Each item has: what's wrong, what the fix looks like, and why it matters. **Re-verified against the codebase after Phases 5–7 landed** (stream processing, drift detection, orchestration, MLflow, manual labelling, automated retraining) — most items from the original audit are now resolved; each has a status note added rather than being deleted, since the original reasoning is still useful context. One new item (#14) was added from a gap found during that Phase 7 work's own code review.

---

## 1. No Dockerfile — CD pipeline is broken

**File:** `services/classifier/` (missing)
**Severity:** Blocks deployment

### What's wrong

The CD workflow (`.github/workflows/cd.yml`) runs `docker/build-push-action` with `context: ./services/classifier` on every push to master. There is no `Dockerfile` in that directory. Every CD run since this workflow was created has failed silently.

Beyond CI, there is no way to containerize and deploy the service. The `host.docker.internal` address in `infra/prometheus/prometheus.yml` exists only because the classifier runs directly on the host instead of as a container — that workaround goes away once the service is containerized.

### What the fix looks like

A multi-stage Dockerfile in `services/classifier/`:

```dockerfile
# Stage 1: dependency layer
FROM python:3.12-slim AS deps
WORKDIR /app
COPY pyproject.toml .
RUN pip install uv && uv pip install --system --no-cache \
    fastapi uvicorn[standard] onnxruntime transformers \
    prometheus-client pydantic

# Stage 2: runtime image (no build tools)
FROM python:3.12-slim
WORKDIR /app
COPY --from=deps /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=deps /usr/local/bin /usr/local/bin
COPY . .
ENV MODEL_PATH=/models/int8
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Multi-stage matters: the build stage can include compilers and wheels; the runtime stage copies only the installed packages. The final image contains no build tools, no pip cache, no source tarballs.

### Why it matters

Without a Dockerfile:
- CD has been broken since it was written
- There is no reproducible way to run the service in any environment
- K8s Deployment manifests cannot reference an image that doesn't exist
- Local dev and production differ in arbitrary ways (Python version, OS, PATH)

---

## 2. `torch` and `optimum` as classifier runtime dependencies

**File:** `services/classifier/pyproject.toml`
**Severity:** Blocks deployment (image size)

### What's wrong

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "transformers>=4.48",
    "torch>=2.2",           # ← 2 GB
    "prometheus-client>=0.21",
    "pydantic>=2.10",
    "optimum[onnxruntime]>=2.1.0",  # ← pulls HF Hub, datasets, accelerate
    "onnxruntime>=1.27.0",
]
```

PyTorch is ~2 GB. Optimum with its transitive dependencies (HuggingFace Hub, `datasets`, `accelerate`) adds several hundred MB more. The classifier at runtime uses none of this — it only needs:
- `onnxruntime` for inference
- `transformers` for the tokenizer (the HuggingFace tokenizer runs without PyTorch)
- `fastapi` + `uvicorn` for the API
- `prometheus-client` for metrics
- `pydantic` for schemas

`torch` and `optimum` belong in `pipelines/optimizer/pyproject.toml` (which doesn't exist yet as a proper package). They are build-time dependencies of the model artifact, not runtime dependencies of the serving binary.

### What the fix looks like

`services/classifier/pyproject.toml` stripped to runtime-only:
```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.34",
    "transformers>=4.48",
    "onnxruntime>=1.27.0",
    "prometheus-client>=0.21",
    "pydantic>=2.10",
]
```

`pipelines/optimizer/pyproject.toml` (new file) carries the heavy deps:
```toml
[project]
name = "sentinel-optimizer"
dependencies = [
    "torch>=2.2",
    "optimum[onnxruntime]>=2.1.0",
    "onnxruntime>=1.27.0",
    "transformers>=4.48",
    "onnx>=1.17",
]
```

The workspace root `pyproject.toml` adds `pipelines/optimizer` to `[tool.uv.workspace] members`.

### Why it matters

Image sizes in production are real operational costs:
- A 5 GB image takes 3–5 minutes to pull on a new node during a scale-out event. A 500 MB image takes 15 seconds.
- Container registries charge for storage and egress.
- K8s node disk is finite. Pulling a 5 GB image per service version fills the node image cache quickly, forcing eviction of other images.
- The attack surface of the running container is proportional to what's installed. PyTorch in a serving container is unnecessary exposure.

---

## 3. No tests for the classifier service

**File:** `tests/conftest.py`, `tests/test_placeholder.py`
**Severity:** Blocks safe deployment

### What's wrong

The test suite has one fixture and one passing placeholder. The `mock_classifier` fixture in `conftest.py` returns `{"label": "LABEL_0", "score": 0.95}` — but the real `Classifier.predict()` returns `{"label": "safe"/"harm", "score": float}`. The mock is already inconsistent with the actual API.

CI runs `pytest` and it passes — vacuously, because there's nothing to test. The CD pipeline deploys after CI passes. This means every deployment is untested.

### What the fix looks like

A `tests/test_classifier_api.py` using FastAPI's `TestClient` (which runs the app in-process without needing a real server):

```python
from fastapi.testclient import TestClient
from unittest.mock import patch

# Patch model loading so tests never need model weights or torch
with patch("model.Classifier") as MockClassifier:
    MockClassifier.return_value.predict.return_value = [{"label": "safe", "score": 0.12}]
    MockClassifier.return_value.model_id = "test-model"
    from main import app

client = TestClient(app)

def test_classify_safe():
    response = client.post("/classify", json={"text": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["label"] in ("safe", "harm")
    assert 0.0 <= body["score"] <= 1.0
    assert body["latency_ms"] >= 0

def test_classify_batch_too_large():
    response = client.post("/classify/batch", json={"texts": ["x"] * 65})
    assert response.status_code == 422  # Pydantic validation rejects > 64

def test_classify_batch_empty():
    response = client.post("/classify/batch", json={"texts": []})
    assert response.status_code == 422

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
```

Minimum test surface: schema validation (bad inputs get 422), happy path (correct structure back), health endpoint, metrics endpoint responds.

Also fix `conftest.py` — the `mock_classifier` return value must match the real output format `{"label": "safe"/"harm", "score": float}`, or remove it if unused.

### Why it matters

Without tests:
- Schema changes silently break clients (you can't tell if `ClassifyResponse` still matches what the model returns)
- Refactors have no safety net
- CI passing means "it didn't crash at import time," not "it works"
- In any team environment, a service with no tests cannot be confidently modified by anyone other than the original author

---

## 4. No liveness / readiness probe split

**Status: ✓ Resolved** — `services/classifier/main.py` now has separate `/health/live` and `/health/ready` routes, and a module-level `_ready` flag set `True` only after `lifespan()`'s warmup completes and `False` again during shutdown — exactly the shape proposed below. `infra/terraform/local/main.tf`'s classifier Deployment wires `livenessProbe`/`readinessProbe` to the two paths.

**File:** `services/classifier/main.py`
**Severity:** Causes operational incidents in K8s

### What's wrong

```python
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": _classifier.model_id if _classifier else None}
```

There is one endpoint that conflates two different questions Kubernetes asks:

**Liveness** (`/health/live`): "Is this process stuck or deadlocked?" If the answer is no for long enough, K8s kills and restarts the pod. This check should almost always return 200. It should only fail if the process has entered an unrecoverable state (event loop deadlocked, OOM killed but still breathing).

**Readiness** (`/health/ready`): "Can this pod handle traffic right now?" If the answer is no, K8s removes the pod from the Service's load balancer pool — no new requests are routed to it. This check should fail during startup (model loading, warmup), during graceful shutdown, and if the model becomes unavailable.

With a single `/health` endpoint:
- If you use it as a liveness probe and warmup takes 10 seconds, K8s will restart the pod during warmup (pod never becomes healthy)
- If you use it as a readiness probe and the model fails to load, the pod stays in the load balancer pool and serves 500s to real traffic

### What the fix looks like

```python
_ready = False  # set to True after warmup completes

@app.get("/health/live")
def liveness() -> dict:
    # Only fail if the process is fundamentally broken
    return {"status": "ok"}

@app.get("/health/ready")
def readiness() -> dict:
    if not _ready:
        raise HTTPException(status_code=503, detail="not ready")
    return {"status": "ok", "model": _classifier.model_id}
```

In the lifespan:
```python
async def lifespan(app):
    global _classifier, _batcher, _ready
    _classifier = Classifier()
    _classifier.warmup()
    _batcher = DynamicBatcher(_classifier.predict)
    _batcher.start()
    _ready = True          # ← only set after warmup
    yield
    _ready = False         # ← unset during shutdown
    _batcher.stop()
```

K8s Deployment spec:
```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
```

### Why it matters

Getting this wrong causes real outages. The most common failure mode: a new deployment rolls out, pods are slow to load the model, K8s liveness kills them before they're ready, the rollout loops forever, and traffic falls back to the old (possibly also broken) pods.

---

## 5. No structured JSON logging

**File:** `services/classifier/main.py`, `pipelines/optimizer/pipeline.py`
**Severity:** Makes production debugging painful

### What's wrong

```python
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s — %(message)s")
```

This produces:
```
2026-06-25 10:23:41,123 INFO services.classifier.model — Loaded roberta | file=model_quantized.onnx | intra_threads=4
```

Every log aggregator (Datadog, CloudWatch, Loki, Splunk, ELK) expects JSON. With text format, you need fragile regex to extract fields. The `run_id=`, `latency_ms=`, `label=` fields embedded in the message string are invisible to the aggregator as structured data — they're just substrings.

### What the fix looks like

Add `python-json-logger` to both `services/classifier/pyproject.toml` and the root dev deps:

```python
# config.py or at the top of main.py / pipeline.py
import logging
from pythonjsonlogger import jsonlogger

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s"
))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
```

Output becomes:
```json
{"asctime": "2026-06-25T10:23:41.123Z", "levelname": "INFO", "name": "model", "message": "Loaded model", "run_id": "abc123", "file": "model_quantized.onnx"}
```

Log fields passed as `extra=` kwargs to `logger.info("msg", extra={"run_id": run_id})` become top-level JSON keys — searchable dimensions, not substring matches.

### Why it matters

When a production incident happens at 2am, the first thing you do is search logs. With JSON logs: `run_id="abc123"` returns exactly the logs for that run. With text logs: you write a regex, it misses edge cases, you waste 20 minutes. At scale, this is not a preference — text logging is simply not parseable by the tooling that exists.

---

## 6. No centralized config (`BaseSettings`)

**Status: ✓ Resolved** — `services/classifier/config.py` exists with a `pydantic_settings.BaseSettings` subclass covering every env var this service reads (`database_url`, `classify_threshold`, `ort_intra_threads`, `max_batch_size`, `max_wait_ms`, `max_queue_depth`, MinIO settings, etc.), imported as `from config import settings` everywhere else in the service — the exact shape proposed below.

**Files:** `services/classifier/model.py`, `services/classifier/batcher.py`
**Severity:** Operational and maintenance burden

### What's wrong

Environment variables are read ad-hoc across multiple files:

```python
# model.py
_THRESHOLD = float(os.environ.get("CLASSIFY_THRESHOLD", "0.5"))
_INTRA_THREADS = int(os.environ.get("ORT_INTRA_THREADS", "4"))
```
```python
# batcher.py
_MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "64"))
_MAX_WAIT_MS = float(os.environ.get("MAX_WAIT_MS", "10"))
```

There is no single place to look at what environment variables this service accepts. There is no type validation at startup. If someone sets `ORT_INTRA_THREADS=four`, the service crashes with a `ValueError` deep in `model.py` on first inference — not at startup, not with a clear error message.

### What the fix looks like

Add `pydantic-settings` (already using pydantic, this is a light addition) and create `services/classifier/config.py`:

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_path: str | None = None
    classify_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    ort_intra_threads: int = Field(default=4, ge=1, le=32)
    max_batch_size: int = Field(default=64, ge=1, le=512)
    max_wait_ms: float = Field(default=10.0, ge=0.0)

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

settings = Settings()
```

Every other module imports `from config import settings`. Type errors, out-of-range values, and missing required variables now fail at process startup with a clear validation error before the service ever accepts a connection.

### Why it matters

In production, config errors are a top cause of deployment failures. With `BaseSettings`, a misconfigured deployment fails immediately at pod startup with a readable error — not 30 minutes later when a specific code path is hit. It also gives you a `.env` file for local dev, which is cleaner than exporting env vars in your shell.

---

## 7. Unbounded asyncio queue in the batcher

**Status: ✓ Resolved** — `batcher.py`'s queue is constructed with `asyncio.Queue(maxsize=settings.max_queue_depth)`, and `submit()` catches `asyncio.QueueFull` and turns it into an HTTP 503 — the exact shape proposed below (down to the config-driven max size).

**File:** `services/classifier/batcher.py`
**Severity:** Causes OOM crashes under load spikes

### What's wrong

```python
self._queue: asyncio.Queue[_Pending] = asyncio.Queue()
```

`asyncio.Queue()` with no `maxsize` is unbounded. If requests arrive faster than the batcher can process them (ORT is slow, CPU is saturated), the queue grows without limit. Each queued item holds the text string plus an asyncio Future. At scale, this is a silent memory leak — the process OOMs and is killed by the OS, rather than degrading gracefully.

### What the fix looks like

```python
_MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "1000"))

self._queue: asyncio.Queue[_Pending] = asyncio.Queue(maxsize=_MAX_QUEUE_DEPTH)
```

In `submit()`:
```python
async def submit(self, text: str) -> dict:
    loop = asyncio.get_running_loop()
    pending = _Pending(text=text, future=loop.create_future())
    try:
        self._queue.put_nowait(pending)
    except asyncio.QueueFull:
        raise HTTPException(status_code=503, detail="queue full, retry later")
    return await pending.future
```

`put_nowait` raises `QueueFull` immediately if the queue is at capacity. The route handler catches it and returns 503. Clients see a retryable error instead of waiting for a request that will eventually time out anyway. This is controlled degradation instead of silent OOM.

Also add a queue depth metric so Prometheus can alert before the queue fills:
```python
QUEUE_DEPTH = Gauge("classifier_queue_depth", "Current number of pending classify requests")
```

### Why it matters

An unbounded queue under load means: latency climbs first (requests wait longer), then memory climbs (queue grows), then the process is killed. By the time the process dies, latency has been bad for minutes. A bounded queue with 503 responses is a faster, clearer signal to upstream callers and load balancers that the service is saturated.

---

## 8. `CLASSIFY_THRESHOLD` baked in at module import time

**Status: Half-resolved — the schema exists, but the value is still discarded.** `model_registry.threshold` is a real column now (`infra/terraform/local/main.tf`'s schema), `services/classifier/db.py`'s `get_active_model()` selects it into the `active` dict, and `register_model()` writes it back on self-registration. But `main.py`'s `lifespan()` never passes `active["threshold"]` into `Classifier(...)` — `Classifier.__init__` unconditionally sets `self.threshold = settings.classify_threshold` (the env var), so the DB-sourced value is fetched and then silently thrown away. The original problem (threshold and model version can drift apart across a promotion) is still real. Fix: thread `active["threshold"]` through to `Classifier.__init__` as an optional override, falling back to `settings.classify_threshold` only when there's no active registry row (matching how `model_dir` already falls back the same way in the same function).

**File:** `services/classifier/model.py`
**Severity:** Operational inflexibility

### What's wrong

```python
_THRESHOLD = float(os.environ.get("CLASSIFY_THRESHOLD", "0.5"))
```

This is read once at module import. With 3 replicas, changing the threshold requires a rolling restart of all pods. More importantly, the threshold is a property of the model, not of the deployment environment — when you swap model versions (Phase 7), the threshold should come from the `model_registry` table alongside the model version. Currently it's decoupled from the model record.

### What the fix looks like

In the Phase 7 model registry design, the `model_registry` table gets a `threshold` column:
```sql
ALTER TABLE model_registry ADD COLUMN threshold FLOAT NOT NULL DEFAULT 0.5;
```

`_resolve_model_dir()` already queries the registry table on startup. It should also return the threshold for that model version, and `Classifier.__init__` stores it as `self.threshold`.

For now (before Phase 7 wires up the DB), the env var approach is acceptable — just move it out of module scope and into `Classifier.__init__` so it's read per-instantiation and documented in `config.py` alongside all other env vars.

### Why it matters

When a new model version has a different decision boundary (fine-tuned on harder examples, for instance), the correct threshold changes. If the threshold lives only in an env var, the model promotion and threshold update become two separate deployments with a window where they're mismatched.

---

## 9. Empty evaluation pipeline

**Status: ✓ Resolved** — both files are fully implemented (`benchmark.py`: scores the held-out set in `datasets/test_dataset.csv`, reports accuracy/precision/recall/F1/AUC-ROC and `peak_memory_mb`; `validate.py`: gates on an absolute `MIN_ACCURACY` floor plus a `MAX_ACCURACY_DROP`-vs-baseline regression check). Wired into both the manual optimizer flow and `pipelines/retraining/pipeline.py`'s automated retrain flow — see [`pipelines/evaluation/explanation.md`](pipelines/evaluation/explanation.md).

**Files:** `pipelines/evaluation/benchmark.py`, `pipelines/evaluation/validate.py`
**Severity:** No automated quality gate before model promotion

### What's wrong

Both files are empty (or contain a single line). There is no automated check that runs after optimization to verify:
- Latency: does the INT8 model meet the <50ms p50 target?
- Accuracy: did quantization degrade accuracy beyond the acceptable 0.2% threshold?

Without these checks, a misconfigured quantization (wrong `weight_type`, missing `extra_options`) or a corrupt artifact can be promoted to the model registry and deployed to production silently.

### What the fix looks like

`pipelines/evaluation/benchmark.py` should:
1. Read the latest `report.json` to find the INT8 checkpoint path (same mechanism as the classifier service)
2. Load the INT8 ONNX model with ORT
3. Run N inference calls on a small fixed dataset with timing
4. Assert p50 latency < 50ms and p99 latency < 150ms
5. Write results to `logs/evaluation/<run-id>/latency.json`
6. Exit with code 1 if assertions fail (so CI/Airflow treats it as a failure)

`pipelines/evaluation/validate.py` should:
1. Load a held-out labeled dataset (even a small 100-sample CSV is enough)
2. Run inference with the INT8 model
3. Compute accuracy, precision, recall, F1
4. Compare against a stored baseline (the FP32 or a previous model version)
5. Fail if accuracy drops more than 0.2%

### Why it matters

Phase 7 (Airflow DAG) will automate retraining and promotion. Without an evaluation gate, the DAG is: retrain → optimize → promote. With an evaluation gate: retrain → optimize → evaluate → promote (only if passing). The evaluation step is the circuit breaker that prevents a bad retrain from reaching production.

---

## 10. `conftest.py` mock is inconsistent with the real API

**Status: ✓ Resolved, differently than proposed** — `tests/conftest.py` was removed entirely rather than fixed in place. `tests/test_classifier_api.py` now patches `main.Classifier` inline with a `MagicMock` whose `predict.side_effect` correctly returns `{"label": "safe"/"harm", "score": float}` for a `list[str]` input — the same contract-correctness this issue asked for, just without a shared fixture file.

**File:** `tests/conftest.py`
**Severity:** Tests give false confidence

### What's wrong

```python
def _classify(text: str):
    return [{"label": "LABEL_0", "score": 0.95}]
```

The real `Classifier.predict()` returns:
```python
[{"label": "safe", "score": 0.12}]  # or "harm"
```

`LABEL_0` is the HuggingFace default label for an untrained model. The actual model maps to `"safe"` and `"harm"` via its `config.json` `id2label`. Any test that uses this fixture and checks `label` values would pass with `LABEL_0` and fail in production.

### What the fix looks like

```python
@pytest.fixture
def mock_predict():
    def _predict(texts: list[str]) -> list[dict]:
        return [{"label": "safe", "score": 0.12}] * len(texts)
    return _predict
```

Note the signature change: the real `predict()` takes a `list[str]`, not a single `str`. The mock must match the real interface exactly — otherwise tests mock the wrong contract.

### Why it matters

A mock that doesn't match the real interface lets tests pass while hiding bugs. The mock's job is to stand in for the real dependency without loading model weights in CI — but it must behave identically to the real thing in terms of input/output contract.

---

## 11. Prometheus scrape target is not K8s-native (deferred to Phase 5)

**Status: Resolved, differently than proposed.** The classifier now runs entirely in-cluster (Phase 5 completed), and `infra/prometheus/prometheus.yml`'s target is `classifier.sentinel-app.svc.cluster.local:8000` — the in-cluster Service DNS name, not `host.k3d.internal`. Service DNS transparently load-balances across however many pods currently back that Service, so this does scale past a single instance, unlike the original workaround. It's not the `kubernetes_sd_configs` pod-level service-discovery this issue originally proposed (no per-pod labels/relabeling), so a future multi-replica setup where you want per-pod metrics (not just an aggregate the Service load-balances you to) would still want that. For this project's current scale, Service-DNS scraping is a legitimate, simpler production pattern, not just a stopgap.

**File:** `infra/prometheus/prometheus.yml`
**Severity:** Does not scale past a single local instance

### What's wrong

```yaml
static_configs:
  - targets: ["host.k3d.internal:8000"]
```

`host.k3d.internal` is a hostname k3d injects into node `/etc/hosts` pointing to the Docker host. This works for local dev because the classifier runs directly on the host. In production on K8s:
- There are multiple replicas (3 pods), each with its own IP
- Pod IPs change on restart
- The host is a K8s node, not a single developer machine

### What the fix looks like

In production, Prometheus discovers targets from the K8s API. Once the classifier is deployed as a K8s Deployment (Phase 5), replace the static config with `kubernetes_sd_configs`:

```yaml
- job_name: classifier
  kubernetes_sd_configs:
    - role: pod
      namespaces:
        names: [sentinel-app]
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_label_app]
      action: keep
      regex: classifier
    - source_labels: [__meta_kubernetes_pod_ip]
      target_label: __address__
      replacement: "$1:8000"
```

This is a Phase 5 item — it requires the classifier to be deployed into the cluster. The `host.k3d.internal` approach is the correct local dev workaround until then.

### Why it matters

The current setup is not portable beyond a single developer machine. Knowing this now avoids building Grafana dashboards around the `host.k3d.internal` label before the right label scheme (`pod_ip`, `instance`) is established.

---

## 12. `pipelines/optimizer/` has no `pyproject.toml` of its own

**File:** `pipelines/optimizer/`
**Severity:** Dependency isolation

### What's wrong

The optimizer pipeline's heavy dependencies (`torch`, `optimum`) are currently in `services/classifier/pyproject.toml` (issue #2). Once those are removed from the classifier, the optimizer has no declared dependencies at all. `pipelines/optimizer/` should be a proper uv workspace member with its own `pyproject.toml`.

### What the fix looks like

Create `pipelines/optimizer/pyproject.toml`:
```toml
[project]
name = "sentinel-optimizer"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "torch>=2.2",
    "optimum[onnxruntime]>=2.1.0",
    "onnxruntime>=1.27.0",
    "transformers>=4.48",
    "onnx>=1.17",
]
```

Update root `pyproject.toml`:
```toml
[tool.uv.workspace]
members = ["services/*", "pipelines/*"]
```

This way `uv sync --package sentinel-classifier` installs only serving deps (fast, small), and `uv sync --package sentinel-optimizer` installs the full ML stack (slow, large). CI for the classifier never downloads PyTorch.

### Why it matters

Dependency isolation between services and pipelines is not organizational preference — it directly determines build times, image sizes, and what breaks when a package is updated. A CI run for a one-line change to `main.py` should not re-download 2 GB of PyTorch wheels.

---

## 13. Retraining on `flagged_content` alone causes class imbalance

**Status: Half-resolved, with a different second mitigation than proposed.** Fix 1 (asymmetric safe-content sampling) is implemented exactly as described: `services/stream-processor/writer.py` stores 100% of `harm` and a `SAFE_SAMPLE_RATE`-fraction (default 0.1) of `safe` content. Fix 2 (auto-mixing with an original balanced dataset in the retrain pipeline) was **not** implemented as proposed — `pipelines/retraining/dataset.py`'s `build_dataset()` deliberately never uses `datasets/test_dataset.csv` as that "original dataset," since that CSV is the evaluation pipeline's held-out set; mixing it into training would contaminate the accuracy numbers the quality gate (#9, now resolved) relies on. An `--initial-dataset-path` hook exists for a *different* balanced CSV if one is ever added, but none ships with the repo, so today's retrains train purely on accepted `flagged_content`. The actual mitigation in place instead is Phase 7's manual-labelling step (`services/label-ui`): a human reviews and explicitly accepts/rejects each candidate before it becomes training data, which catches a badly-skewed batch that pure automatic mixing wouldn't — but unlike automatic mixing, this depends on an operator actually paying attention to the ratio, and nothing currently blocks or warns on submitting a fine-tune run whose accepted set is heavily skewed toward one label. The failure mode this issue describes (a retrain that collapses toward predicting one class) is exactly what the quality gate's accuracy floor is designed to catch after the fact — but nothing warns *before* training that the input is skewed.

**Files:** `pipelines/retrain/` (Phase 7), stream processor (Phase 5)
**Severity:** Model quality — silent accuracy collapse after first retrain

### What's wrong

`flagged_content` in MongoDB stores only `harm` predictions. If the retrain pipeline trains exclusively on this collection, the dataset has a single label. A binary classifier trained on one class will collapse — it learns to predict `harm` for every input regardless of content, achieving 100% recall and 0% precision.

Even with both classes stored, MongoDB will accumulate far more harmful samples than safe ones if the harm rate is low (e.g. 5% of traffic) — the ratio skews badly over time.

### What the fix looks like

**Two changes that must both be in place:**

**1. Stream processor samples safe content into MongoDB**

When the stream processor (Phase 5) writes to MongoDB, it uses asymmetric sampling:

```python
SAFE_SAMPLE_RATE = float(os.environ.get("SAFE_SAMPLE_RATE", "0.1"))

if result["label"] == "harm":
    mongo.flagged_content.insert_one(doc)   # 100% of harm
elif random.random() < SAFE_SAMPLE_RATE:
    mongo.flagged_content.insert_one(doc)   # ~10% of safe
```

If traffic is 5% harmful, sampling 10% of safe gives a collection that is roughly 33% harm / 67% safe — far better than 100% harm. Tune `SAFE_SAMPLE_RATE` so the resulting ratio stays within 20/80 worst-case.

**2. Retrain pipeline mixes `flagged_content` with the original training dataset**

The Airflow `retrain_dag` (Phase 7) must never train exclusively on the MongoDB collection. The retrain script combines:

```python
# Load original balanced dataset from HuggingFace (or MinIO archive)
original = load_dataset("VijayRam1812/content-classifier-roberta-dataset")

# Load new samples accumulated since last retrain
new_samples = mongo.flagged_content.find({"ts": {"$gt": last_retrain_ts}})

# Combine: original provides balance, new samples teach new patterns
combined = concat_datasets([original, new_samples_as_dataset])
trainer.train(combined)
```

The original dataset is the stable base. `flagged_content` is augmentation, not a replacement. The ratio should be roughly 80% original / 20% new samples, adjustable by the number of new examples available.

### Why it matters

The first retrain after accumulating enough harmful examples will silently destroy model quality if this isn't handled. PSI > 0.2 triggers the retrain, the retrain produces a harm-biased model, the model gets promoted, and the classifier starts flagging everything as harmful — including safe content. This is worse than no retraining at all. The fix must be in place before Phase 7 is built.

---

## 14. `drift_dag.py` can't distinguish a pipeline crash from a benign "nothing to report"

**File:** `orchestration/drift_dag.py`, `pipelines/drift/drift_job.py`
**Severity:** Silent monitoring gap — a real outage produces no alert
**Found:** during code review of the Phase 7 automated-retraining PR

### What's wrong

`drift_job.py` deliberately exits `0` for two very different situations: "ran cleanly, found no drift" and "skipped — not enough reference data yet, or no rows in the current window" (see `MIN_REFERENCE_SIZE`). It exits `1` for a genuine configuration/DB error, and `2` when drift **is** detected — but at the Kubernetes/`spark-operator` layer, both `1` and `2` surface identically as `applicationState.state == "FAILED"`; there's no way to tell "drift was found" apart from "the job crashed" from that field alone.

`drift_dag.py`'s `check_drift` task works around the *drift-vs-crash* half of this ambiguity correctly by reading `drift_stats` directly instead of trusting the K8s-level status (`drift_job.py` always writes its row before calling `sys.exit()`, so Postgres is the real source of truth). But it has no way to work around the other half: a stale-or-missing `drift_stats` row means either "genuinely nothing new to report" (benign) or "the job crashed before writing anything" (a real outage) — both look identical to `check_drift`, and both correctly fall back to `no_drift_detected` (the safe choice — never retrain on an ambiguous signal). The cost of that safety is observability: a real, ongoing failure in the drift pipeline (bad `DATABASE_URL` after a credential rotation, a Spark driver OOM, a regression in `drift_job.py` itself) currently produces the exact same "quiet, uneventful hourly run" as everything working perfectly with nothing to report. Nothing pages anyone.

### What the fix looks like

The distinction needs to live in `drift_stats` itself, not be inferred from its absence. Add an explicit status column:

```sql
ALTER TABLE drift_stats ADD COLUMN status VARCHAR(10) NOT NULL DEFAULT 'ok'
    CHECK (status IN ('ok', 'skipped', 'error'));
```

`drift_job.py` writes a row in **all three** cases instead of only the two it does today — `'ok'` for a completed comparison (drift found or not, `drift_flagged` already captures which), `'skipped'` for the `MIN_REFERENCE_SIZE`/empty-window guards, and `'error'` for a caught exception right before re-raising (a `try/except` around the body of `main()`, writing the row, then exiting 1 as it already does). `check_drift` then reads `status` directly instead of inferring it from row freshness: `'error'` becomes a real Airflow task failure (visible in the DAG's own success/failure history, alertable via Airflow's own failure notifications), `'skipped'`/`'ok'`-with-`drift_flagged=false` both correctly still mean `no_drift_detected`.

### Why it matters

Silent monitoring gaps are worse than loud failures — a broken drift pipeline that "looks fine" for weeks means no one notices until someone asks "wait, why hasn't this model been retrained in 3 months?" and discovers the automation quietly stopped working. This is exactly the kind of gap that's cheap to close while building the feature and expensive to discover later.

---

## Status tracker

Work through these in order — each one unblocks the next.

| # | Issue | Priority | Status |
|---|-------|----------|--------|
| 1 | Dockerfile for classifier | P0 — CD is broken | ✓ done |
| 2 | Remove `torch`/`optimum` from classifier deps | P0 — image size | ✓ done |
| 3 | Tests for classifier service | P0 — no safety net | ✓ done |
| 4 | Liveness / readiness probe split | P1 — K8s stability | ✓ done |
| 5 | Structured JSON logging | P1 — production observability | open |
| 6 | Centralized config (`BaseSettings`) | P1 — operational safety | ✓ done |
| 7 | Bounded queue with backpressure in batcher | P1 — OOM risk | ✓ done |
| 8 | Threshold from model registry, not env var | P2 — model/config coupling | half-done — see #8 |
| 9 | Implement evaluation pipeline | P2 — no quality gate | ✓ done |
| 10 | Fix `conftest.py` mock contract | P2 — false test confidence | ✓ done (differently — see #10) |
| 11 | K8s-native Prometheus scrape (deferred to Phase 5) | P3 — deferred until classifier is in cluster | ✓ done (differently — see #11) |
| 12 | `pyproject.toml` for optimizer pipeline | P2 — dependency isolation | ✓ done |
| 13 | Class imbalance in retrain dataset | P1 — model collapses after first retrain | half-done — see #13 |
| 14 | `drift_dag.py` can't distinguish a crash from "nothing to report" | P2 — silent monitoring gap | open |

**Genuinely still open:** #5 (JSON logging), #8 (threshold override wiring), #14 (drift status column). **Open, differently scoped than described:** #13 (an operator now curates the data, but nothing warns on a skewed accepted-label batch before training).
