# Sentinel — Phase 0 Concepts, Tricks & Tips

Everything implemented in Phase 0, explained from the ground up with the non-obvious
lessons highlighted. Read this when you want to explain a decision in an interview or
understand why the code is written the way it is.

---

## 1. ONNX and Model Optimization

### What ONNX is

ONNX (Open Neural Network Exchange) is a file format and runtime for running ML
models without their original training framework. When you export a PyTorch model to
ONNX, you get a computation graph — a directed acyclic graph of mathematical operations
(matmul, relu, softmax…) serialized to a `.onnx` file. ONNX Runtime (ORT) executes
that graph, often faster than PyTorch's own inference path.

```
PyTorch model (.pt)  →  export  →  model.onnx  →  ORT  →  predictions
```

Why bother? PyTorch carries a lot of training machinery at inference time (autograd,
optimizer state, Python GIL). ONNX strips all of that. For a RoBERTa model doing
binary classification, ORT is typically 30–50% faster than `model.eval()` in PyTorch
on CPU.

### The three variants we benchmark

| Variant | What it is | Typical speed | Typical size |
|---|---|---|---|
| PyTorch FP32 | Raw PyTorch, `model.eval()` | baseline | ~500 MB |
| ONNX O2 | ONNX graph with O2 optimizations | 1.3–1.5× | ~480 MB |
| ONNX INT8 | O2 + INT8 dynamic quantization | 2–4× | ~120 MB |

**O2 graph optimization** applies a set of graph rewrites that ONNX Runtime's optimizer
knows are always safe: constant folding (pre-compute anything that never changes),
operator fusion (merge a Conv + BatchNorm into one op), layout optimization (reshape
tensors to favor SIMD). You don't lose any precision.

**INT8 dynamic quantization** converts weight matrices from 32-bit floats to 8-bit
integers at export time. At inference time, input activations are quantized per-batch
dynamically (hence "dynamic" — not calibrated on a dataset upfront). The quantized
matmul does 4 operations per CPU instruction instead of 1, which is why it's faster.
The tradeoff: you lose a small amount of accuracy because 8-bit has less precision.
For safety classification this is usually acceptable (1–2% accuracy drop).

### The ORT session options that matter

```python
so = ort.SessionOptions()
so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
so.intra_op_num_threads = 4   # threads within one op (e.g., a matmul)
so.inter_op_num_threads = 1   # threads across ops (we have 1 request at a time)
so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL  # no parallel op execution
so.enable_mem_pattern = True  # reuse memory allocations across calls
so.enable_cpu_mem_arena = True  # pre-allocate a memory arena instead of malloc/free
```

**Why `intra_op_num_threads = 4` and `inter_op_num_threads = 1`?**
For a single-request inference (one sequence at a time), parallelizing within a matmul
(intra) is useful. Parallelizing across ops (inter) adds overhead with no benefit
because RoBERTa's ops are sequential — each layer feeds into the next. Setting inter
to >1 causes thread contention.

**Trick:** When you have multiple concurrent requests (e.g., 8 simultaneous classify
calls), flip this: `intra=1, inter=8`. Each request gets one thread; they all run in
parallel. For Sentinel's 2-replica deployment, `intra=4` is the right call because
each pod handles one request at a time from the stream processor.

### Benchmarking: what we measure

The classifier is binary: it outputs `harmful` (class 1) or `safe` (class 0).
The benchmark computes three metrics across the entire test dataset:

- **Accuracy** — fraction of correct predictions (misleading on imbalanced datasets)
- **F1** — harmonic mean of precision and recall; the right metric when false
  negatives (missed harmful content) are costly
- **AUC-ROC** — area under the ROC curve; measures how well the model separates the
  two classes across all decision thresholds, regardless of the threshold you pick

These three are computed for each of the three variants (PyTorch FP32, ONNX O2,
ONNX INT8) so you can see whether quantization degrades recall for harmful content.

**The bug we fixed:** the original code called `make_ort_session()` inside a loop
over the dataset — one session creation per row. Loading an ONNX model takes ~2
seconds. That's 7,560 seconds (over 2 hours) for 3780 rows vs the correct approach:
create the session once, run inference over the entire dataset in one pass.

```python
# Wrong — creates a new ORT session for every row
df["pred"] = [make_ort_session(path)(text) for text in texts]

# Right — one session, one pass over all 3780 rows
_, all_preds, _ = run_ort_dataset(sess_q, encoded_ort)
df["pred"] = all_preds
```

### Version naming: why timestamp alone is fragile

A Unix timestamp truncated to 6 digits gives you a "unique" number that repeats every
`999999` seconds = ~11.5 days. Truncating further (last 6 chars = modulo 1M) means it
collides every ~277 hours. In practice, running the optimization script twice within
16 minutes gives the same version string, and the second `INSERT` into `model_registry`
would fail silently (or overwrite the first, depending on ON CONFLICT behavior).

The fix: `v{YYYYMMDD}-{git-sha}`. The date prevents collisions across days. The git
SHA pins the exact code state. If git is unavailable (CI without checkout), fall back
to `local` — you know it's a development build.

```python
def _model_version() -> str:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        sha = "local"
    return f"v{date}-{sha}"
```

---

## 2. FastAPI: Sync vs Async and the Thread Pool

### The two modes FastAPI supports

```python
# Async route — runs directly on the event loop
@app.post("/classify")
async def classify(request: ClassifyRequest):
    result = await some_async_operation()  # yields control back to event loop
    return result

# Sync route — FastAPI offloads to a thread pool
@app.post("/classify")
def classify(request: ClassifyRequest):
    result = SESSION.run(...)  # blocking C call, ~35ms
    return result
```

### Why the classifier uses a sync route

`SESSION.run()` is a call into ONNX Runtime's C++ engine. It blocks the calling
thread for ~35ms. There is no async version — you cannot `await` a C call.

If you put this in an `async def` route:
1. FastAPI calls the function on the event loop thread
2. `SESSION.run()` blocks that thread for 35ms
3. During those 35ms, NO other request can be handled — not health checks, not
   other /classify calls, nothing
4. At high load, requests queue up behind each other instead of running concurrently

If you put this in a `def` route:
1. FastAPI detects it's a sync function
2. It runs it in a thread pool (default: 40 threads via `anyio`)
3. The event loop is free during the 35ms blocking call
4. Other requests are handled concurrently on other threads
5. 40 concurrent classify calls run in parallel, each on their own thread

**Rule of thumb:** use `async def` for I/O-bound operations where you can genuinely
`await` something (database queries with an async driver, external HTTP calls with
`httpx`, etc.). Use `def` for CPU-bound operations that call synchronous libraries.

### How uvicorn's thread pool works

When uvicorn runs a sync route, it uses `anyio.to_thread.run_sync()` internally,
which draws from a `ThreadPoolExecutor`. The default pool size is 40.

```
Request 1 → thread pool → classify() runs on Thread-1 (35ms)
Request 2 → thread pool → classify() runs on Thread-2 (35ms) — concurrent!
Request 3 → thread pool → classify() runs on Thread-3 (35ms) — concurrent!
```

For Sentinel, the stream processor sends one request at a time, so we never stress
the thread pool. But knowing this matters when someone asks "how does it scale?"

### Pydantic models: validation at the boundary

```python
class ClassifyRequest(BaseModel):
    text: str
    trace_id: str | None = None
```

Pydantic validates the incoming JSON against this schema before your function sees
it. If the request body is `{"text": 123}`, Pydantic coerces `123` to `"123"`.
If it's `{}` (no `text` key), you get a 422 Unprocessable Entity before your code
runs. This is input validation at the system boundary — you trust the validated
`request.text` without defensive checks inside the function.

**Tip:** `str | None = None` is Python 3.10+ union syntax. In older codebases you'd
see `Optional[str] = None`. They're equivalent; use `str | None` in new code.

### The /reload antipattern

The original design had a `/reload` endpoint that would reload the model in-place.
This is wrong for two reasons:

1. **Multi-replica split:** When you have 2 replicas behind a Service, calling
   `/reload` hits one replica (round-robin load balancing). The other replica stays
   on the old model. Now you have two replicas serving different model versions —
   silently, with no error.

2. **Race condition:** If a request hits the pod during the reload (while the old
   session is being replaced), you get a race between `SESSION.run()` and
   `SESSION = new_session`.

The fix: rolling restart. You update the model version in `model_registry`, then
`kubectl rollout restart deployment/classifier`. Kubernetes replaces each pod one
at a time. Each new pod calls `_load_active_model()` on startup and gets the new
version from the registry. Old pods keep serving until the new ones pass health
checks. Zero downtime, no split state.

---

## 3. Prometheus Metrics

### The four metric types

**Counter** — monotonically increasing number. Never decreases (except on reset).
Use for: total requests, total errors, total bytes processed.

```python
CLASSIFICATION_TOTAL = Counter(
    "sentinel_classifications_total",
    "Total classifications performed",
    ["result"],  # labels: result="harmful" or result="safe"
)
CLASSIFICATION_TOTAL.labels(result=label).inc()
```

**Gauge** — a value that can go up or down. Use for: current active connections,
queue depth, memory usage, number of in-flight requests.

```python
MODEL_VERSION = Gauge(
    "sentinel_model_version_info",
    "Currently loaded model version",
    ["version"],
)
MODEL_VERSION.labels(version="v20240101-abc1234").set(1)
```

**Histogram** — samples a distribution. Records observations in configurable
buckets. Use for: request latency, payload size, confidence scores.

```python
INFERENCE_LATENCY = Histogram(
    "sentinel_classification_latency_seconds",
    "Toxicity classification inference latency",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
INFERENCE_LATENCY.observe(latency_seconds)
```

This creates three series automatically:
- `_bucket{le="0.05"}` — count of observations ≤ 50ms
- `_sum` — sum of all observations
- `_count` — number of observations

From these three you can compute any percentile in PromQL.

**Summary** — like a histogram but computes quantiles client-side. Avoid it: you
can't aggregate summaries across replicas (p99 of (p99_a + p99_b) ≠ p99 of combined).
Always use Histogram when you need percentiles.

### PromQL essentials

```promql
# rate() — per-second rate of a counter over a window
# Counters only go up; rate() computes the per-second increase
rate(sentinel_classifications_total[5m])

# [5m] is a range vector selector — gives you 5 minutes of data points
# Prometheus scrapes every 15s by default, so [5m] = ~20 data points

# histogram_quantile() — compute a percentile from histogram buckets
histogram_quantile(0.95, rate(sentinel_classification_latency_seconds_bucket[5m]))
# The _bucket suffix is required — it's one of the three auto-generated series

# Label filtering
rate(sentinel_classifications_total{result="harmful"}[5m])

# Ratio (harmful fraction)
rate(sentinel_classifications_total{result="harmful"}[5m])
/ rate(sentinel_classifications_total[5m])
```

**Why `rate()` and not just the counter value?**
Counters reset to 0 when the pod restarts. `rate()` handles resets correctly by
detecting when the counter decreases and treating it as a reset, not a negative rate.
If you use the raw counter value and graph it, pod restarts create ugly vertical
drops and the trend is meaningless.

### The `/metrics` endpoint pattern

```python
from prometheus_client import make_asgi_app
app.mount("/metrics", make_asgi_app())
```

`make_asgi_app()` creates a WSGI/ASGI endpoint that responds to GET /metrics with
all registered metrics in the Prometheus text exposition format. Prometheus scrapes
this endpoint every 15 seconds (configurable). ServiceMonitor tells Prometheus
which pods to scrape and on which port.

**Trick:** The default registry is global. All `Counter`, `Gauge`, `Histogram`
objects you create anywhere in the module are automatically registered and appear
in `/metrics`. You don't need to pass them anywhere.

---

## 4. PostgreSQL Schema Design

### The classifications table

```sql
CREATE TABLE classifications (
    id          BIGSERIAL PRIMARY KEY,
    trace_id    TEXT,
    session_id  TEXT,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    label       TEXT NOT NULL,
    confidence  REAL NOT NULL,
    model_version TEXT NOT NULL,
    prompt_len  INTEGER,
    response_len INTEGER
);
```

**Why `BIGSERIAL` not `UUID`?**
BIGSERIAL is an auto-incrementing 64-bit integer. It's faster to index and join on
than a 128-bit UUID, and for an append-only log table, sequential IDs make sense.
Use UUIDs when you need IDs generated client-side (distributed systems, mobile apps)
or when you need IDs to be non-guessable.

**Why `TIMESTAMPTZ` not `TIMESTAMP`?**
`TIMESTAMP` stores the literal time string with no timezone info. If your app moves
servers or changes timezone settings, your historical timestamps become ambiguous.
`TIMESTAMPTZ` stores UTC internally and displays in the session timezone. Always use
`TIMESTAMPTZ`.

### Indexes and why they exist

```sql
CREATE INDEX idx_classifications_ts ON classifications(ts DESC);
CREATE INDEX idx_classifications_label_ts ON classifications(label, ts DESC);
CREATE INDEX idx_classifications_model_version_ts ON classifications(model_version, ts DESC);
```

**Why these three?**

The three most common queries against this table:
1. "Show me the last N classifications" — needs `ts DESC` index
2. "Show me harmful classifications in the last hour" — needs `(label, ts)` composite
3. "Show me accuracy drift for model version X over time" — needs `(model_version, ts)`

**Composite index ordering matters:** `(label, ts DESC)` allows a query like
`WHERE label = 'harmful' AND ts > NOW() - INTERVAL '1 hour'` to use the index.
The equality condition on `label` narrows the range, then `ts DESC` orders within it.
Reversing to `(ts, label)` would force a full scan of the time range followed by
a filter on label.

**Tip — `DESC` in indexes:** PostgreSQL can scan an index forward or backward, so
`DESC` in an index declaration usually doesn't matter for range queries. It matters
when you need `ORDER BY ts DESC LIMIT 10` — with `DESC` the planner can satisfy this
with an index scan without a sort step.

### model_registry: the status constraint

```sql
status TEXT NOT NULL DEFAULT 'staging'
    CHECK (status IN ('staging', 'active', 'retired'))
```

A CHECK constraint enforced at the DB level means no application code can insert
`status = 'production'` or `status = ''` by mistake. The DB is the last line of
defense. The single `active` model invariant — at most one row with `status='active'`
at a time — is enforced in the retrain DAG (which sets old active to `retired` before
promoting new), not with a UNIQUE constraint (there may briefly be zero active rows
during a swap).

---

## 5. GitHub Actions CI/CD

### Job structure and parallelism

```yaml
jobs:
  lint:    # ruff check + ruff format --check
  test:    # pytest tests/unit
  terraform-validate:  # terraform fmt -check + init + validate
  docker-build:        # build classifier image (no push)
```

These four jobs run in parallel by default because none has `needs:` on another.
Parallel jobs = faster CI. If `lint` fails, `test` still runs (different job).
If you want test to only run after lint passes, add `needs: lint` to the test job.

**Tip:** For the typical workflow — don't block tests on lint. Let them run in
parallel. The PR author sees all failures at once instead of serially ("fix lint,
push, wait 3 min, now see test failures").

### The difference between CI and CD

`ci.yml` runs on every push and pull_request. Its job is to tell you if the code
is correct. It should never have side effects on production.

`cd.yml` runs on push to `main` only. It builds and pushes the Docker image to
GHCR. It has side effects — it changes the container registry, which is a shared
resource. This is why CI and CD are separate files.

### GHCR and the automatic GITHUB_TOKEN

```yaml
- uses: docker/login-action@v3
  with:
    registry: ghcr.io
    username: ${{ github.actor }}
    password: ${{ secrets.GITHUB_TOKEN }}
```

`GITHUB_TOKEN` is automatically created for each workflow run by GitHub. You don't
create it in Settings → Secrets. It has read/write access to the repository's
packages (GHCR) by default. The token expires when the workflow job finishes.

**Tip:** If your push to GHCR fails with a 403, check the repository's package
settings (Settings → Packages → visibility). New repositories default to private
packages; you may need to set them to public or grant the token write access.

### Multi-tag pattern

```yaml
tags: |
  ghcr.io/vjayram/sentinel/classifier:sha-${{ github.sha }}
  ghcr.io/vjayram/sentinel/classifier:latest
```

Two tags per build:
- `sha-abc1234` — immutable, points to exactly this commit's image forever
- `latest` — mutable, always points to the most recent main build

Use `sha-...` tags in Kubernetes Deployment specs (`image: ...:sha-abc1234`).
This makes deployments deterministic and auditable. If a deployment breaks, you
know exactly which image is running and can roll back to the previous SHA tag.
Never deploy `:latest` in production — you can't tell what code is running.

### Docker layer caching

```yaml
- uses: docker/build-push-action@v6
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

`type=gha` uses GitHub Actions cache for Docker layer caching. On a cache hit,
layers that haven't changed (e.g., your requirements.txt install layer) are
reused without re-downloading. A typical classifier image build goes from 4 min
(cold) to 45 seconds (warm cache) with this.

**How Docker layer caching works:** Each `RUN`, `COPY`, `ADD` instruction in a
Dockerfile is a layer. Docker caches each layer keyed by the instruction + all
previous layers. If `COPY requirements.txt .` doesn't change, `RUN pip install`
is skipped. This is why you always copy requirements before source code:

```dockerfile
COPY requirements.txt .          # layer 5 — only changes when deps change
RUN pip install -r requirements.txt  # layer 6 — cached unless layer 5 changed
COPY . .                         # layer 7 — changes on every source change
```

If you did `COPY . .` first, every source change invalidates the pip install layer.

---

## 6. The model_registry Table as Source of Truth

### Why consult the DB on startup instead of env vars

If you always load the model path from `MODEL_PATH` env var, then deploying a new
model version requires updating the Kubernetes Deployment manifest and doing a
rolling restart. That's fine — but it means the Deployment manifest is the source
of truth for which model is active, not the registry.

By querying `model_registry` on startup:
```python
SELECT onnx_path, version FROM model_registry
WHERE status = 'active'
ORDER BY deployed_at DESC NULLS LAST
LIMIT 1
```

The registry becomes the source of truth. The Deployment manifest doesn't need to
change when you promote a new model version. The retrain DAG updates the registry,
triggers a rolling restart, and each new pod picks up the new model automatically.

**Graceful fallback:** if the DB is unreachable at pod startup (network partition,
PostgreSQL down), fall back to `MODEL_PATH` env var. This makes the startup not
dependent on DB availability, while still preferring the registry when it's healthy.

### The rolling restart pattern

```bash
# Airflow retrain_dag, after promoting new model version:
kubectl rollout restart deployment/classifier -n sentinel-app
```

Kubernetes replaces pods one at a time (RollingUpdate strategy, `maxUnavailable=0`).
Each new pod:
1. Runs the initContainer: downloads new ONNX model from MinIO
2. Starts the classifier: calls `_load_active_model()`, gets new version from registry
3. Passes liveness probe (`/health` returns 200)
4. Receives traffic; old pod is terminated

This gives you zero-downtime model upgrades with no code changes to the classifier.
The registry owns the routing decision.

---

## Key Lessons Summary

| Concept | The lesson |
|---|---|
| ONNX vs PyTorch inference | ONNX Runtime is 1.3–4× faster because it strips training machinery and can optimize the graph |
| INT8 quantization | 4× smaller, 2–4× faster, ~1–2% accuracy cost — measure per category, not just overall |
| ORT session options | `intra_op=4, inter_op=1` for single-request workloads; flip for concurrent workloads |
| Sync vs async FastAPI | CPU-bound blocking code → `def`. Awaitable I/O → `async def`. Never block the event loop |
| Prometheus Counter | Use `rate()`, not raw values — handles pod restarts and resets correctly |
| Prometheus Histogram | `_bucket`, `_sum`, `_count` are the three auto-generated series; use `histogram_quantile` for percentiles |
| Composite indexes | Put equality columns first, range/order columns last: `(label, ts DESC)` not `(ts, label)` |
| TIMESTAMPTZ | Always use it over TIMESTAMP — timezone-safe, unambiguous in UTC internally |
| CI vs CD | CI validates (no side effects). CD deploys (modifies shared state). Keep them separate |
| Docker layer ordering | Copy dependency files before source code — cache the expensive pip install layer |
| latest tag | Never use `:latest` in K8s Deployments — use immutable SHA tags for auditability |
| /reload antipattern | Multi-replica deployments need rolling restarts, not in-process reload — avoids silent version splits |
| Source of truth | model_registry as the single source of truth for active model — not env vars, not manifests |
