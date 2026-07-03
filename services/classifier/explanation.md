# Classifier Service — Explanation

The classifier is a long-running FastAPI service that accepts text, runs it
through a fine-tuned RoBERTa ONNX model, and returns a `harm`/`safe` label with
a confidence score. It is the only component in Sentinel that touches the ONNX
runtime — everything else calls this service's HTTP API.

---

## Directory structure

```
services/classifier/
  main.py      — FastAPI app, lifespan, routes, async persistence
  model.py     — ORT session, tokenizer, inference, version naming
  batcher.py   — Dynamic batching engine
  db.py        — asyncpg pool, model registry queries, classification writes
  download.py  — MinIO model download with local cache
  metrics.py   — Prometheus metrics and log-to-metric bridge
  schemas.py   — Pydantic request/response models
```

---

## How to run

```bash
cd services/classifier
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --log-level info
```

`dev-start.sh` runs this automatically after resolving the model from the
registry and opening all port-forwards. Override the model explicitly with:

```bash
MODEL_PATH=/path/to/int8 uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## schemas.py

Defines the wire format for all API endpoints using Pydantic v2. Pydantic
validates and coerces incoming JSON before any route handler runs — invalid
requests return 422 with field-level error messages, not Python tracebacks.

### `MAX_BATCH_SIZE = 64`

A module-level constant imported by both the schema (to constrain the request
field) and `batcher.py` (to cap the internal batch size). Defined once so the
two layers can never drift out of sync — if you raise the limit, both enforce
the new value automatically.

### `ClassifyResult`

```python
class ClassifyResult(BaseModel):
    label: str
    score: float
```

The minimal unit of output. Defined as its own model rather than inlined into
the response types because it appears inside both `ClassifyResponse` and
`BatchClassifyResponse`. Duplication would create drift risk when the shape changes.

### `ClassifyResponse(ClassifyResult)`

Inherits `label` and `score` from `ClassifyResult`, adds `latency_ms`,
`model_version`, and `inference_at`. Inheritance is correct here because a
single-text response is exactly a result plus per-request metadata.

### `BatchClassifyRequest`

```python
class BatchClassifyRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    persist: bool = True
```

In Pydantic v2, `min_length`/`max_length` on a `list` field constrain the number
of items, not string length. Sending an empty list or more than 64 texts returns
a 422 before any inference runs.

**`persist: bool = True`** — added in Phase 5. When the stream processor calls
the batch endpoint, it sets `persist=False` so the classifier skips its own
async PostgreSQL write. The stream processor then writes directly to PG with
`span_id` for idempotency. Any other caller (curl, tests, other services) leaves
this at the default `True` and the classifier persists as it always has.

### `BatchClassifyResponse`

Returns `results` (one per input text, in the same order as the request),
`latency_ms` (total wall time for the entire batch), `batch_size` (echo of the
input count — lets callers verify they got a result for every input without
counting the array), and `model_version` (which the stream processor extracts
to write to PG).

---

## model.py

Owns the ORT inference session and the tokenizer. Everything outside this file
calls `Classifier.predict()` — no other module imports ORT or transformers.

### `_THRESHOLD` and `_INTRA_THREADS`

Read from environment variables at module import time, before any class is
instantiated. This means they are fixed for the lifetime of the process. To
change them, restart the service.

- **`CLASSIFY_THRESHOLD`** (default `0.5`): sigmoid/softmax cutoff. Score ≥ this
  → `harm`. Raising it makes the classifier more conservative (fewer false
  positives, more false negatives). Lowering it catches more harmful content
  at the cost of flagging more safe content.
- **`ORT_INTRA_THREADS`** (default `4`): CPU thread count for matrix multiply
  kernels inside a single ORT op. For a single-request workload, `4` is good —
  one MatMul uses 4 cores in parallel. If many requests arrive concurrently (e.g.,
  the stream processor sending batches rapidly), set `ORT_INTRA_THREADS=1` and
  let OS scheduling handle the concurrency — otherwise 10 concurrent requests
  each trying to use 4 threads thrash the CPU.

### `_sigmoid(x)`

```python
return 1.0 / (1.0 + np.exp(-x))
```

Maps an unbounded logit to `[0, 1]`. Used for single-logit binary classification
heads. Sigmoid is the correct activation when the model was fine-tuned with binary
cross-entropy loss and produces one output per sample.

### `_project_root()`

Walks up from `__file__` looking for `uv.lock`. This locates the workspace root
regardless of what directory `uvicorn` is started from. Anchoring paths to
`__file__` (a fixed property of the code's location) rather than `os.getcwd()`
(an ambient runtime property) is the correct production pattern — it works the
same whether you run from `services/classifier/`, `~/projects/sentinel/`, or as
a K8s container where the working directory is arbitrary.

### `_resolve_model_dir()`

Two-path local resolution for when the DB registry is not available:

1. **`MODEL_PATH` env var** — explicit override. Returns the path, derives
   `deployed_at` from the ONNX file's mtime, and passes `source_model_id=None`
   (the HuggingFace ID is not known from just a local path).
2. **Auto-detect from `logs/optimizer/`** — scans for `report.json` files,
   sorts by `completed_at` (ISO 8601 strings sort lexicographically so no
   parsing is needed), picks the most recent, and reads the quantize stage's
   output directory and the original `model_id`. This is the developer
   convenience path so you don't need to copy-paste a run ID after every
   optimizer run.

In normal operation (with `DATABASE_URL` set), the lifespan calls `db.get_active_model()`
and passes `model_dir` to the constructor directly — `_resolve_model_dir` is
only called as a last resort when the DB is unreachable or the registry is empty.

### `Classifier.__init__(model_dir)` — two construction paths

```python
def __init__(self, model_dir: Path | None = None) -> None:
    if model_dir is not None:
        # lifespan already resolved dir from DB + MinIO download
        model_dir = Path(model_dir)
        onnx_file = next(model_dir.glob("*.onnx"), None)
        mtime = ...
        resolved_dir, deployed_at, source_model_id = model_dir, ts.strftime(...), None
    else:
        # fallback: resolve locally from MODEL_PATH or logs/optimizer/
        resolved_dir, deployed_at, source_model_id = _resolve_model_dir()
```

When `lifespan` resolves the model from the registry and downloads it from MinIO,
it passes the resulting local path as `model_dir`. The constructor trusts this
path and skips local discovery. When DB is unreachable, `lifespan` passes
`model_dir=None` and the constructor runs its own discovery.

### ORT `SessionOptions`

```python
opts.intra_op_num_threads = _INTRA_THREADS   # parallelism within one op
opts.inter_op_num_threads = 1                # ops run sequentially
opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
```

`ORT_SEQUENTIAL` + `inter_op_num_threads=1` means ORT executes one operation at
a time. Transformer inference is a linear chain of ops (attention → norm → FFN
→ attention...) with no branching — there is no inter-op parallelism to exploit.
`intra_op_num_threads` parallelizes the work inside a single op (e.g., splits a
matrix multiply across 4 CPU cores).

**Tip:** inspect the actual execution plan with:
```python
import onnxruntime as ort
sess = ort.InferenceSession("model_quantized.onnx")
print(sess.get_inputs())   # what the model expects
print(sess.get_outputs())  # what it produces
```

### `self._input_names`

```python
self._input_names = {inp.name for inp in self._session.get_inputs()}
```

Built once at startup. In `predict()`, the tokenizer's output dict is filtered
to only keys the ORT session expects:

```python
ort_inputs = {k: v for k, v in inputs.items() if k in self._input_names}
```

RoBERTa does not use `token_type_ids` (unlike BERT), but HuggingFace's tokenizer
may still return them. Passing an unexpected input name to `session.run()` raises
an error. This filter makes inference robust to tokenizer version differences
without hardcoding input name lists.

### Model version naming

```python
quant_tag = model_dir.name   # "int8", "o2", or "fp32"
self.model_version = f"sentinel-roberta-{deployed_at}-{quant_tag}"
```

`model_dir.name` is the last path component of the model directory — which is
always the quantization stage name (`int8/`, `o2/`, `fp32/`). The resulting
version string (`sentinel-roberta-20260627T003749Z-int8`) is human-readable,
sortable, and encodes both when the model was deployed and which optimization
stage is running.

### `predict(texts)` — handling both output shapes

```python
logits = self._session.run(["logits"], ort_inputs)[0]

if logits.shape[-1] == 1:
    # Single-logit binary head: sigmoid gives P(harm) directly.
    scores = _sigmoid(logits).squeeze(axis=-1)
else:
    # Multi-class softmax head: take P(last class) as the harm score.
    exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
    scores = (exp / exp.sum(axis=-1, keepdims=True))[:, -1]
```

Fine-tuned models come in two shapes:
- **`(batch, 1)` — single logit**: the model uses a single output neuron with
  binary cross-entropy loss. Sigmoid converts the logit to `P(harm)`.
- **`(batch, N)` — multi-class**: the model has a softmax head with N class
  outputs. We take the last class as the harm probability, following the
  standard HuggingFace convention where label ordering goes from least to
  most severe (e.g., `{0: "safe", 1: "harm"}`).

The two-branch approach makes the classifier compatible with either model
architecture without code changes — just swap the model artifacts.

The numerically stable softmax is `exp(x - max(x)) / sum(exp(x - max(x)))`.
Subtracting `max(x)` before exp prevents overflow for large logits (e.g., if
a logit is 500, `exp(500)` would be inf without the shift).

**Tokenizer parameters in `predict()`:**
- `padding=True` — pads all inputs to the longest sequence in the batch.
  Without padding, sequences of different lengths cannot form a rectangular
  input tensor.
- `truncation=True, max_length=512` — silently truncates at 512 tokens.
  RoBERTa's positional embedding table has exactly 512 entries; longer sequences
  would index out of bounds.
- `return_tensors="np"` — returns NumPy arrays, avoiding a PyTorch allocation
  that ORT would have to convert.

### `warmup()`

Calls `predict(["warmup"])` once during lifespan startup. ORT JIT-compiles the
graph on the first call (100–300ms for INT8 RoBERTa). Without warmup, the first
real request pays this cost after the service is already marked ready. Warmup
ensures the JIT cost is paid before traffic arrives.

---

## batcher.py

### Why dynamic batching exists

ORT's `session.run()` on N texts is far cheaper than N individual calls. The
matrix multiply kernels in each transformer layer operate on the full batch
simultaneously — setup overhead (memory allocation, SIMD initialization, kernel
scheduling) is paid once. For N=8, a single batched call typically runs 2–4×
faster than 8 serial calls.

The problem: the FastAPI `/classify` route serves one request at a time. Without
batching, every concurrent request gets its own `session.run()` call, achieving
the worst possible throughput.

`DynamicBatcher` groups concurrent single-text requests into batches
automatically, with no changes required to how clients call the API.

### `_Pending` dataclass

```python
@dataclass
class _Pending:
    text: str
    future: asyncio.Future = field(default_factory=asyncio.Future)
```

Pairs each input text with the `asyncio.Future` that will carry its result back
to the waiting route handler. Using `asyncio.Future` (not a queue or event) lets
the route handler `await` exactly its own result — no polling, no shared state.

`field(default_factory=asyncio.Future)` — the factory is called per-instance at
construction time. If written as `future: asyncio.Future = asyncio.Future()`,
the `Future()` call would execute at class definition time and every `_Pending`
would share the same Future object (a classic Python mutable-default-argument bug
applied to dataclasses).

### `asyncio.Queue`

Thread-safe within a single event loop. Route handlers (`submit()`) put items in;
the `_loop` coroutine drains them. The queue is unbounded — at extremely high
load, unprocessed requests accumulate in memory. A production version would cap
the queue and return 503 when the backlog exceeds a threshold.

### `_loop()` — the batching algorithm

```
1. Block on queue.get() — wait for the first request
2. Set deadline: now + MAX_WAIT_MS
3. Pull from queue until MAX_BATCH_SIZE reached or deadline expires
4. Run ORT inference in a thread pool executor
5. Set each Future's result (or exception)
6. Immediately loop back to step 1
```

Step 6 is continuous — there is no idle gap between batches. Any requests that
arrived while the previous batch was executing in the thread pool are immediately
collected into the next batch. Under sustained load, the queue is never empty and
batches stay at or near `MAX_BATCH_SIZE`.

### `MAX_WAIT_MS = 10`

The window for batch collection. If only one request arrives and no more come
within 10ms, the batcher flushes a batch of 1 rather than waiting indefinitely.
This caps the extra latency batching adds to single requests. Under high load,
the queue fills faster than 10ms and the deadline is never reached — `MAX_WAIT_MS`
only matters at low traffic where every millisecond of wait would be wasted.

**Tip:** tune `MAX_WAIT_MS` based on your traffic pattern:
- High, bursty traffic → increase to 20–50ms to harvest larger batches
- Low, latency-sensitive traffic → decrease to 2–5ms
- Set `MAX_BATCH_SIZE=1` to disable batching entirely (useful for benchmarking)

### `run_in_executor(None, self._predict, ...)`

`session.run()` is a blocking C extension call. Calling it directly inside the
async `_loop` coroutine would block the event loop — no other coroutines (route
handlers, heartbeats, pending Futures) could progress until inference completes.
`run_in_executor(None, ...)` submits the call to Python's default thread pool,
suspends `_loop` at the `await`, and resumes it when the thread finishes. The
event loop remains responsive throughout the entire inference window.

### Exception propagation

```python
except Exception as exc:
    for pending in batch:
        if not pending.future.done():
            pending.future.set_exception(exc)
```

If ORT raises (e.g., out of memory, corrupt model file), the exception is set on
every Future in the batch. FastAPI converts unresolved Future exceptions into 500
responses. Without this handler, a single ORT failure would leave every request
in the batch suspended forever — the route handler would never unblock and
connections would time out.

### `/classify` is `async def` — both routes are

Both `/classify` and `/classify/batch` are declared `async def`. The distinction
is what they do inside:

- `/classify` awaits `_batcher.submit()` — a coroutine that puts one item in the
  queue and waits for its Future. No blocking I/O directly.
- `/classify/batch` calls `loop.run_in_executor(None, _classifier.predict, ...)` —
  offloads the blocking ORT call to a thread and awaits the result.

Both approaches keep the event loop unblocked during inference. The key rule:
**never call a blocking C function directly in an `async def` function without
`run_in_executor`**.

---

## db.py

Wraps `asyncpg` (an async PostgreSQL driver) with four focused functions. The
pool is initialized once in `lifespan` and shared across all requests.

### `init_pool(dsn)`

```python
pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=5)
```

- `min_size=1` — one connection always kept warm. Prevents the first request
  after a period of inactivity from paying a connection setup cost.
- `max_size=5` — at most 5 concurrent connections. The classifier is mostly
  compute-bound (ORT inference), not I/O-bound, so a deep connection pool would
  waste PostgreSQL connection resources without improving throughput.
- `command_timeout=5` — any query taking longer than 5 seconds is cancelled.
  Without a timeout, a slow PG query would hold a pool slot indefinitely, and
  under sustained load all 5 slots would fill with stuck queries.

The DSN is logged with the password stripped (`dsn.split("@")[-1]` drops
everything before the `@`, leaving just `host:port/db`).

### `get_active_model(pool)`

```sql
SELECT model_version, model_path, threshold
FROM model_registry
WHERE status IN ('active', 'staging')
ORDER BY
    CASE status WHEN 'active' THEN 0 ELSE 1 END,
    COALESCE(promoted_at, created_at) DESC
LIMIT 1
```

Priority ranking in one query:
1. `active` rows always beat `staging` rows (CASE expression)
2. Among rows of equal status, the most recently promoted/created wins

This means:
- In production (Airflow running): the Airflow-promoted `active` model is served.
- In local dev (no Airflow): the most recent `staging` model from the optimizer
  is served. No special dev-mode code path needed.

`COALESCE(promoted_at, created_at)` handles `staging` rows that have never been
promoted — they have `promoted_at = NULL`, so fall back to `created_at`.

### `register_model(pool, model_version, model_path, threshold)`

```sql
INSERT INTO model_registry (model_version, model_path, threshold, status)
VALUES ($1, $2, $3, 'active')
ON CONFLICT (model_version) DO NOTHING
```

Called during lifespan startup, after the model is loaded. The `ON CONFLICT DO
NOTHING` makes it safe to call on every pod startup — in a multi-replica
deployment, the first pod registers the version, subsequent pods skip it silently.

Note: inserts as `'active'` — this is intentional for the local dev flow where
Airflow is not present. In production with Airflow managing promotions, this
insert would conflict with the Airflow-managed row and do nothing (the Airflow
row is already `active`).

### `write_classification` and `write_classifications_batch`

Two write paths for single and batch requests respectively.
`write_classifications_batch` uses `asyncpg.executemany()` — sends all rows in
a single network round-trip to PostgreSQL rather than one INSERT per row.

Note: these writes are **fire-and-forget** from the route handler's perspective.
The route creates an `asyncio.Task` for the write and returns the response
immediately. If the write fails (PG down, FK constraint violation), the task logs
the exception but the route already returned 200. This is an acceptable trade-off
for low-latency inference — classification results are the primary product;
persistence is secondary.

When `persist=False` is set (stream processor calls), these functions are not
called at all. The stream processor owns PG writes for those requests.

---

## download.py

Resolves a `model_registry.model_path` string to a local directory ready for
`Classifier.__init__()`. Handles two formats the registry can contain.

### Two model_path formats

**MinIO path** (normal case): `"models/<run-id>/int8/model_quantized.onnx"`

Written by the optimizer when MinIO was reachable. The download function:
1. Parses the path into `(bucket="models", prefix="<run-id>/int8/")`
2. Checks the local cache — `/tmp/sentinel-model-cache/<run-id>/int8/`
3. On cache miss: calls `s3.list_objects_v2(Prefix=prefix)` to list all files in
   the stage directory, then downloads each one.

**Local path** (fallback): `"/absolute/path/to/artifacts/<run-id>/int8"`

Written by the optimizer when MinIO was unreachable. The function checks that
the path exists and contains an `.onnx` file, then returns it directly.

### `_parse_minio_path(model_path)`

```python
parts = model_path.split("/")
bucket = parts[0]
prefix = "/".join(parts[1:-1]) + "/"
```

Input: `"models/abc-123/int8/model_quantized.onnx"`
Output: `("models", "abc-123/int8/")`

The prefix ends with `/` so `list_objects_v2` returns all files in the directory
(`abc-123/int8/model_quantized.onnx`, `abc-123/int8/tokenizer.json`, etc.), not
just the one file named in `model_path`.

### `_CACHE_ROOT = Path("/tmp/sentinel-model-cache")`

Using `/tmp` means the cache is pod-local and does not survive pod restarts. This
is intentional — downloading the model on every cold start is acceptable (it's
~120 MB for INT8, typically 10–30s) and avoids the complexity of a shared
persistent cache volume. After the first download, the cache is warm and
subsequent classifier restarts within the same pod lifetime are instant.

Override with `MODEL_CACHE_DIR` env var if you want the cache in a mounted PVC.

### boto3 client setup

```python
boto3.client(
    "s3",
    endpoint_url=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
    aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "sentinel"),
    aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "sentinel-minio"),
    config=Config(signature_version="s3v4", connect_timeout=5, retries={"max_attempts": 2}),
    region_name="us-east-1",
)
```

`endpoint_url` overrides boto3's default AWS endpoint, pointing it at MinIO
instead. `region_name="us-east-1"` is required by the S3v4 signature algorithm
even though MinIO ignores the region — without it, the client raises a
configuration error.

`retries={"max_attempts": 2}` — try the download twice before giving up. This
handles transient network blips inside the cluster. More retries would delay
startup too much if MinIO is genuinely down.

### Cache hit logic

```python
if cache_dir.exists() and any(cache_dir.glob("*.onnx")):
    logger.info("Model cache hit | dir=%s", cache_dir)
    return cache_dir
```

The presence of any `.onnx` file is used as the cache validity signal, not a
hash or timestamp. This is intentional simplicity — models are immutable once
written to MinIO (keyed by run ID). The same `<run-id>/int8/` prefix always
contains the same bytes. A more robust implementation would compare file counts
or check a manifest, but for this use case the simple check is sufficient.

**Tip:** force a re-download by clearing the cache:
```bash
rm -rf /tmp/sentinel-model-cache/
```

---

## metrics.py

### `REQUEST_COUNT` — Counter

```python
Counter("classifier_requests_total", ..., ["endpoint", "label"])
```

Two label dimensions:
- `endpoint`: `"classify"` or `"classify_batch"` — which path received the request
- `label`: `"safe"` or `"harm"` — classification outcome

Useful PromQL:
```promql
# Harm rate over the last 5 minutes
sum(rate(classifier_requests_total{label="harm"}[5m]))
  / sum(rate(classifier_requests_total[5m]))

# Traffic by endpoint
sum(rate(classifier_requests_total[5m])) by (endpoint)
```

### `REQUEST_LATENCY` — Histogram

```python
Histogram(..., buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0])
```

Bucket boundaries are chosen around the INT8 model's expected latency (~35ms
p50): dense below 100ms for precision, coarser above. Every observation is
counted into every bucket ≥ its value (Prometheus histograms are cumulative).

**Changing buckets** requires a service restart — bucket boundaries are fixed
at registration time. If you find your p99 is usually above 500ms, add a `2.0`
bucket to distinguish "slow" from "very slow" rather than having everything pile
up in the `+Inf` bucket.

### `BATCH_SIZE` — Histogram

```python
Histogram("classifier_batch_size", ..., buckets=[1, 2, 4, 8, 16, 32, 64])
```

Tracks the size of each `/classify/batch` call (the external HTTP batch, not
the dynamic batcher's internal batches). Powers-of-2 buckets match ML workload
patterns where clients tend to double their batch sizes rather than increase
linearly.

A p90 near 64 signals the service is at capacity — clients are consistently
hitting the maximum. The `ClassifierBatchBackpressure` alert in Prometheus rules
fires when this threshold is sustained for 5 minutes.

### `LOG_ERRORS` — Counter + `_PrometheusLogHandler`

```python
class _PrometheusLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            LOG_ERRORS.labels(level=record.levelname).inc()
```

Attaches to the root logger so it intercepts ERROR and CRITICAL records from
any module in the process. This bridges the structured logging world and the
metrics world — you can alert on error rates in Prometheus without shipping
logs to a separate aggregator (Loki, Elasticsearch).

`attach_log_handler()` is called after `basicConfig()` in `main.py`. Order
matters: `basicConfig` must run first to configure the root handler's level.
Adding the Prometheus handler after ensures it inherits the level filter.

---

## main.py

### `lifespan` — startup sequence

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _classifier, _batcher, _pool

    # 1. Open DB pool and query the model registry
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        _pool = await _db.init_pool(dsn)
        active = await _db.get_active_model(_pool)
        if active:
            # download_model is blocking boto3 I/O — run in thread
            loop = asyncio.get_running_loop()
            model_dir = await loop.run_in_executor(None, download_model, active["model_path"])

    # 2. Load the model (falls back to local discovery if model_dir is None)
    _classifier = Classifier(model_dir=model_dir)
    _classifier.warmup()

    # 3. Register this version in the registry (idempotent)
    if _pool:
        await _db.register_model(_pool, _classifier.model_version, ...)

    # 4. Start the dynamic batcher
    _batcher = DynamicBatcher(_classifier.predict)
    _batcher.start()

    yield  # — service handles requests here —

    _batcher.stop()
    if _pool:
        await _db.close_pool(_pool)
```

`lifespan` replaced `@app.on_event("startup")` in FastAPI 0.93+. Code before
`yield` runs at startup; code after `yield` at shutdown. The `global` declarations
give route handlers (defined at module scope) access to the shared instances.

**`download_model` in `run_in_executor`** — boto3's `download_file` is blocking
synchronous I/O. Calling it directly in `async def lifespan` would block the
entire event loop during the download (~10–30s for the INT8 model). Running it
in the executor offloads the I/O to a thread and keeps the event loop responsive.

**Why `register_model` inserts as `'active'`** — the local dev workflow has no
Airflow to promote models. The classifier registers itself as `active` on startup.
In production with Airflow, this `INSERT ... ON CONFLICT DO NOTHING` is a no-op
because the registry row already exists at `active` status from the promotion step.

### `app.mount("/metrics", make_asgi_app())`

Mounts the Prometheus ASGI application at `/metrics`. `make_asgi_app()` returns
a standard ASGI app that serves all registered metrics in Prometheus text format.
Mounting it avoids running a separate HTTP server on a different port.

Prometheus scrape config uses `metrics_path: /metrics/` (trailing slash) — FastAPI
redirects `/metrics` → `/metrics/`. Specifying the final path avoids the redirect
round-trip on every scrape.

### `_persist_single` and `_persist_batch`

Both are called via `asyncio.create_task(...)` — fire-and-forget from the route
handler's perspective. The route returns the response immediately without waiting
for the DB write to complete.

```python
if _pool and request.persist:
    records = [...]
    asyncio.create_task(_persist_batch(records))
```

`request.persist` (default `True`) gates the write. When the stream processor
calls with `persist=False`, no task is created and the route has no DB side
effect — the stream processor is responsible for PG writes on those requests.

### Environment variables summary

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | unset | asyncpg DSN; enables registry + persistence |
| `MINIO_ENDPOINT` | `http://localhost:9000` | MinIO S3 API endpoint |
| `MINIO_ACCESS_KEY` | `sentinel` | MinIO access key |
| `MINIO_SECRET_KEY` | `sentinel-minio` | MinIO secret key |
| `MODEL_CACHE_DIR` | `/tmp/sentinel-model-cache` | Local MinIO download cache |
| `MODEL_PATH` | unset | Explicit model dir; skips registry lookup |
| `CLASSIFY_THRESHOLD` | `0.5` | Score cutoff for `harm` label |
| `ORT_INTRA_THREADS` | `4` | CPU threads per ORT matrix op |
| `MAX_BATCH_SIZE` | `64` | Max texts per dynamic batch |
| `MAX_WAIT_MS` | `10` | Max ms to wait filling a batch |
