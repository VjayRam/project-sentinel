# Classifier Service — Component Explanations

## Directory structure

```
services/classifier/
  main.py      — FastAPI app, routes, lifespan
  model.py     — ORT session, tokenizer, inference logic
  batcher.py   — Dynamic batching engine
  metrics.py   — Prometheus metrics and log handler
  schemas.py   — Pydantic request/response models
```

## How to run

```bash
cd services/classifier
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

The service auto-selects the most recent optimization run. Override with:

```bash
MODEL_PATH=/absolute/path/to/int8/dir uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## schemas.py

Defines the wire format for all API endpoints using Pydantic v2.

### `MAX_BATCH_SIZE = 64`

A module-level constant shared between the schema (`BatchClassifyRequest` field constraint) and imported by the batcher. Defined once so they can never drift apart.

### `ClassifyRequest`

Single-text input. The `text` field has no length constraint deliberately — truncation is handled in `model.py` at tokenization time (`max_length=512`). Enforcing length in the schema would be redundant and would reject inputs that are valid after truncation.

### `ClassifyResult`

The minimal unit of output: a label string and a confidence score. Defined as its own model (not inlined into responses) because it appears inside both `ClassifyResponse` and `BatchClassifyResponse`. Duplication would create drift risk.

### `ClassifyResponse(ClassifyResult)`

Inherits `label` and `score` from `ClassifyResult` and adds `latency_ms`. Inheritance is correct here — a single classify response is a result plus metadata.

### `BatchClassifyRequest`

```python
texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
```

In Pydantic v2, `min_length` and `max_length` on a `list` field constrain the number of items (not string length). This enforces: at least 1 text and at most 64. Sending an empty list or more than 64 texts returns a 422 before any inference runs.

### `BatchClassifyResponse`

Returns `results` (one per input text, in the same order), `latency_ms` (total wall time for the entire batch), and `batch_size` (echo of how many texts were processed). The `batch_size` echo lets clients verify they got results for all inputs without counting the results array.

---

## model.py

Owns the ORT session, tokenizer, and the inference contract. Everything below lifespan knows nothing about ORT — it only calls `Classifier.predict()`.

### `_THRESHOLD` and `_INTRA_THREADS`

Read from environment variables at module import time, not inside `__init__`. This means they are set once at process start and never change. If you need to change them, restart the service — no dynamic reconfiguration.

- `CLASSIFY_THRESHOLD` (default `0.5`): sigmoid score cutoff. Above this → `harm`. Raising it makes the classifier more conservative (fewer false positives); lowering it catches more harmful content (fewer false negatives). Adjust based on precision/recall requirements.
- `ORT_INTRA_THREADS` (default `4`): threads per ORT op. With `4`, a single MatMul uses 4 CPU threads. With concurrent requests, set to `1` so requests don't compete for CPU cores.

### `_sigmoid`

```python
return 1.0 / (1.0 + np.exp(-x))
```

This model outputs a single logit per sample (not two-class softmax). It was fine-tuned with binary cross-entropy loss, which produces a single output neuron. Sigmoid maps the unbounded logit to `[0, 1]`, where values above the threshold indicate harm. Using softmax here would be wrong — softmax requires at least two outputs to be meaningful.

### `_project_root()`

Walks up the directory tree from `__file__` until it finds a `uv.lock` file, which marks the workspace root. This makes all relative paths in the service (logs, model artifacts) resolve correctly regardless of what directory you launch `uvicorn` from. Anchoring to `__file__` instead of `os.getcwd()` is the correct production pattern — CWD is an ambient runtime property; `__file__` is a fixed property of the code's location.

### `_resolve_model_dir()`

Two-path resolution:

1. **Explicit**: if `MODEL_PATH` env var is set, use it. This is the production path — Kubernetes sets this via a Deployment env block pointing to the MinIO-fetched model.
2. **Auto-detect**: scans `logs/optimizer/*/report.json`, sorts by `completed_at` (ISO 8601 strings sort lexicographically), takes the most recent, and reads the `stages.quantize.output` path. This is the developer convenience path so you don't need to copy-paste a run ID after every optimization run.

### ORT `SessionOptions`

```python
opts.intra_op_num_threads = _INTRA_THREADS   # parallelism within one op
opts.inter_op_num_threads = 1                # ops run sequentially
opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
```

`ORT_SEQUENTIAL` with `inter=1` means ORT executes one op at a time. This is correct for transformer inference where the ops form a linear chain — there is no parallelism to exploit between ops. `intra=4` parallelizes the matrix multiply kernels themselves across CPU cores.

For high-concurrency deployments where many requests arrive simultaneously: set `ORT_INTRA_THREADS=1`. This lets the OS schedule multiple requests in parallel threads without contention on CPU cores.

### `self._input_names`

```python
self._input_names = {inp.name for inp in self._session.get_inputs()}
```

Built once at startup. In `predict()`, tokenizer outputs are filtered to only the names the session actually expects:

```python
ort_inputs = {k: v for k, v in inputs.items() if k in self._input_names}
```

RoBERTa doesn't use `token_type_ids` (unlike BERT), but the HuggingFace tokenizer may still return them. Passing an unexpected key to `session.run()` raises an error. This filter makes the inference code robust to tokenizer version differences without hardcoding input name lists.

### `warmup()`

Calls `predict(["warmup"])` once during lifespan startup. ORT JIT-compiles the graph on the first call, which can take 100–300ms depending on the model size. Without warmup, the first real request pays this cost. Warmup ensures the cost is paid before the service is considered ready to serve traffic.

### `predict(texts: list[str])`

The only public method. Accepts a list of strings, returns a list of dicts in the same order.

- `padding=True`: pads all inputs to the length of the longest in the batch. Without padding, sequences of different lengths cannot be batched.
- `truncation=True, max_length=512`: silently truncates inputs longer than 512 tokens. RoBERTa's positional embedding table only covers 512 positions — passing longer sequences would error.
- `return_tensors="np"`: returns NumPy arrays directly, avoiding a PyTorch tensor allocation that ORT would have to convert anyway.
- `scores.squeeze(axis=-1)`: removes the trailing size-1 dimension from `(batch, 1)` logits to get a flat `(batch,)` array.

---

## batcher.py

### Why dynamic batching exists

ORT's `session.run()` on a batch of N texts is significantly cheaper than N individual `session.run()` calls. The matrix multiplies in each transformer layer operate on the full batch simultaneously — the GPU/CPU kernel overhead (scheduling, memory allocation, SIMD setup) is paid once instead of N times. For N=8 texts, a single batched call is typically 2–4x faster than 8 individual calls.

The problem: the FastAPI routes serve one request at a time. Without batching, every request gets its own `session.run()` call regardless of how many concurrent requests are waiting.

The `DynamicBatcher` solves this by grouping concurrent requests into batches automatically, without requiring clients to implement batching themselves.

### `_Pending` dataclass

```python
@dataclass
class _Pending:
    text: str
    future: asyncio.Future = field(default_factory=asyncio.Future)
```

A pairing of the input text with the `asyncio.Future` that will hold its result. Using `asyncio.Future` (not `asyncio.Event` or a queue) lets the route handler `await` exactly one result — its own — without polling or shared state.

`field(default_factory=asyncio.Future)` creates a new Future for each instance. If the default were `asyncio.Future()` (called at class definition time), every `_Pending` would share the same Future object.

### `asyncio.Queue`

`asyncio.Queue` is thread-safe within a single event loop. Route handlers put requests in; the `_loop` task consumes them. The queue is unbounded by default — if the batcher falls behind, requests accumulate in memory. In production, you would cap the queue and return 503 when it exceeds a threshold (backpressure).

### `_loop()` — the batching algorithm

```
1. Block on queue.get() until at least one request arrives
2. Set a deadline: now + MAX_WAIT_MS
3. Keep pulling from queue until MAX_BATCH_SIZE or deadline, whichever comes first
4. Run ORT inference in a thread pool
5. Resolve each Future with its result
6. Immediately loop back to step 1
```

Step 6 is the "continuous" part — there is no idle gap between batches. The instant a batch finishes, the loop checks the queue again. Any requests that arrived during the previous batch's inference are immediately grouped into the next batch.

### `MAX_WAIT_MS = 10`

The time budget for batch collection. If only 1 request arrives and no more come within 10ms, the batcher flushes a batch of 1 rather than waiting indefinitely. This caps the additional latency imposed by batching. Under high load, the queue fills faster than 10ms and the batch reaches `MAX_BATCH_SIZE` before the deadline fires — the `MAX_WAIT_MS` budget is never fully consumed.

### `run_in_executor`

```python
results = await loop.run_in_executor(None, self._predict, [p.text for p in batch])
```

`session.run()` is a blocking C call. Calling it directly in the async `_loop` coroutine would block the event loop — no other coroutines (including incoming requests) could run until inference finishes. `run_in_executor(None, ...)` submits the call to the default thread pool, suspends `_loop` at the `await`, and resumes it when the thread finishes. The event loop remains responsive throughout.

### Exception propagation

```python
except Exception as exc:
    for pending in batch:
        if not pending.future.done():
            pending.future.set_exception(exc)
```

If ORT raises (e.g., out-of-memory, corrupt model), the exception is propagated to every waiting Future in the batch. FastAPI converts unhandled Future exceptions into 500 responses. Without this, a single ORT failure would leave every request in the batch suspended forever.

### Why `/classify` is `async def` but `/classify/batch` is `sync def`

`/classify` is `async def` because it awaits a Future — it does not block on ORT directly.

`/classify/batch` is `sync def` because it calls `_classifier.predict()` (blocking ORT) directly. FastAPI runs sync routes in a thread pool automatically, so it does not block the event loop. The distinction: async routes that block ORT would block the event loop; sync routes that block ORT run in a thread.

---

## metrics.py

### `REQUEST_COUNT` — Counter

```python
Counter("classifier_requests_total", ..., ["endpoint", "label"])
```

A counter that only ever increases. Labels:
- `endpoint`: `"classify"` or `"classify_batch"` — tells you which path the traffic is coming through
- `label`: `"safe"` or `"harm"` — tells you the distribution of classification outcomes

Useful PromQL: `rate(classifier_requests_total[5m])` gives requests per second. `sum by (label)(rate(...))` shows the harm/safe split rate.

### `REQUEST_LATENCY` — Histogram

```python
Histogram("classifier_request_latency_seconds", ..., ["endpoint"],
          buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0])
```

A histogram counts observations into predefined buckets. Buckets are right-inclusive: `le=0.025` counts requests that completed in ≤25ms. The bucket boundaries are chosen around the INT8 model's expected latency (~35ms p50): fine-grained below 100ms, coarser above.

Prometheus histograms support `histogram_quantile(0.95, rate(...bucket[5m]))` for percentile computation. The recording rules in `infra/prometheus/rules/classifier.yml` pre-compute p50/p95/p99 so Grafana panels don't run expensive quantile queries on every refresh.

### `BATCH_SIZE` — Histogram

Tracks how many texts are in each `/classify/batch` call. The buckets `[1, 2, 4, 8, 16, 32, 64]` are powers of 2 — useful because batch sizes in ML workloads tend to double rather than increase linearly. A p90 near 64 means clients are consistently hitting the max and the system may be capacity-constrained.

### `LOG_ERRORS` — Counter + `_PrometheusLogHandler`

```python
class _PrometheusLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            LOG_ERRORS.labels(level=record.levelname).inc()
```

Attaches to the root logger (`logging.getLogger()`) so it intercepts ERROR and CRITICAL records from any module in the process. This surfaces log errors in Prometheus without requiring a separate log aggregator (Loki, ELK). `classifier_log_errors_total` feeds the `ClassifierLogErrors` and `ClassifierCriticalErrors` alerts in the rules file.

`attach_log_handler()` is called once in `main.py` immediately after `basicConfig`. Order matters: `basicConfig` must run first to configure the root handler's level; adding the Prometheus handler after means it inherits the root level filter.

---

## main.py

### `lifespan` context manager

```python
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
```

`lifespan` replaced the deprecated `@app.on_event("startup")` pattern in FastAPI 0.93+. Code before `yield` runs at startup; code after `yield` runs at shutdown. The `global` declarations allow the route handlers (defined at module level) to access the shared instances.

Startup sequence:
1. Load ORT session and tokenizer (`Classifier()`)
2. Run one warmup inference (pays JIT cost)
3. Start the background batching loop (`DynamicBatcher.start()`)

Shutdown sequence (after `yield`):
1. Cancel the batching task (`DynamicBatcher.stop()`)
2. Drop the classifier reference (lets GC free the ~500MB model memory)

### `app.mount("/metrics", make_asgi_app())`

Mounts the Prometheus ASGI application at `/metrics`. The Prometheus client library's `make_asgi_app()` returns a standard ASGI app that serves all registered metrics in the Prometheus text exposition format. Mounting it avoids running a separate HTTP server on a different port.

Note: FastAPI redirects `/metrics` → `/metrics/` (trailing slash). Prometheus scrape config uses `metrics_path: /metrics/` to avoid the redirect.

### Environment variables summary

| Variable | Default | Effect |
|----------|---------|--------|
| `MODEL_PATH` | unset | Explicit path to INT8 model dir; overrides auto-detect |
| `CLASSIFY_THRESHOLD` | `0.5` | Sigmoid cutoff for harm classification |
| `ORT_INTRA_THREADS` | `4` | CPU threads per ORT op |
| `MAX_BATCH_SIZE` | `64` | Max texts per dynamic batch |
| `MAX_WAIT_MS` | `10` | Max ms to wait filling a batch |
