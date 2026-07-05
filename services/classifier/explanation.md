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
  config.py    — pydantic-settings Settings — every tunable env var, one place
```

All the env-var-driven knobs described throughout this file (`CLASSIFY_THRESHOLD`,
`ORT_INTRA_THREADS`, `MAX_BATCH_SIZE`, `MAX_WAIT_MS`, `MAX_QUEUE_DEPTH`,
`MINIO_*`, `MODEL_PATH`, `DATABASE_URL`) are defined once in `config.py`'s
`Settings` class (pydantic-settings, reads `.env` + real env vars, validates
ranges via `Field(ge=..., le=...)` at process startup instead of failing
deep inside `batcher.py` or `model.py` on first use) and imported as the
single `settings` object everywhere else — `from config import settings`.
`schemas.py`'s `MAX_BATCH_SIZE` constant and `batcher.py`'s batching limit
both read `settings.max_batch_size`, so the two can never drift apart.

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

### `MAX_BATCH_SIZE = settings.max_batch_size`

```python
MAX_BATCH_SIZE = settings.max_batch_size
```

Originally a hardcoded `64` in this file. Now reads from `config.py`'s
`Settings` (env var `MAX_BATCH_SIZE`, default `64`) — the same object
`batcher.py`'s `DynamicBatcher` reads for its actual batching limit. Before
this unification, the request-validation cap here and the batcher's runtime
cap were two separate constants that happened to agree; raising one without
the other would have silently let requests larger than the batcher was
tuned for pass validation, or rejected requests the batcher could have
handled fine. One `settings.max_batch_size` value now drives both.

`/classify`, `/classify/batch`, and their `ClassifyRequest`/`ClassifyResponse`/
`ClassifyResult`/`BatchClassifyRequest`/`BatchClassifyResponse` schemas were
removed — nothing in the system called them (the stream processor always
called `/v1/moderations`, and no other caller existed), and `/v1/moderations`
already accepts both a single string and a list on its own. See the
`ModerationRequest` section below for how skip-persist is signaled now that
no schema in this file has a `persist` field at all (moved to an HTTP header
specifically so it wouldn't need to live in a request body schema meant to
stay OpenAI-shaped).

---

## OpenAI Moderation API-compatible types (`/v1/moderations`)

```python
class ModerationRequest(BaseModel):
    input: str | Annotated[list[str], Field(min_length=1, max_length=MAX_BATCH_SIZE)]

class ModerationCategories(BaseModel):
    harm: bool

class ModerationCategoryScores(BaseModel):
    harm: float

class ModerationResult(BaseModel):
    flagged: bool
    categories: ModerationCategories
    category_scores: ModerationCategoryScores

class ModerationResponse(BaseModel):
    id: str      # "modr-<hex>"
    model: str   # model version string
    results: list[ModerationResult]
```

Shaped to match `openai.moderations.create()`'s request/response contract
exactly — `input` accepts either a single string or a list (mirroring the
real API), and the response nests `categories`/`category_scores` per result
the same way OpenAI's does, just with one category (`harm`) instead of
OpenAI's fixed taxonomy. This is the **only** classifier endpoint — every
caller, internal (stream processor) and external, uses it. See `main.py`'s
`moderate()` route below for how it branches internally on `str` vs `list`
input, and for why this schema deliberately has **no** Sentinel-internal
fields (like the old `persist` flag) — a clean OpenAI-compatible surface
with zero fields an external caller would need to know or care about.

---

## model.py

Owns the ORT inference session and the tokenizer. Everything outside this file
calls `Classifier.predict()` — no other module imports ORT or transformers.

### Threshold and thread count come from `config.settings`, not raw env vars

Read via `config.py`'s `Settings` object at import time, before any class is
instantiated — not `os.environ.get(...)` scattered through this file. This
means they are fixed for the lifetime of the process (to change them,
restart the service), and validated once at startup (`Field(ge=..., le=...)`)
rather than potentially failing deep inside a request.

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

### `_deployed_at_from_dir(model_dir)`

```python
def _deployed_at_from_dir(model_dir: Path) -> str:
    onnx_file = next(model_dir.glob("*.onnx"), None)
    mtime = onnx_file.stat().st_mtime if onnx_file else None
    ts = datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else datetime.now(timezone.utc)
    return ts.strftime("%Y%m%dT%H%M%SZ")
```

Extracted as its own helper because both construction paths need "a
`deployed_at` timestamp derived from this directory's `.onnx` file mtime" —
before this existed, that logic was duplicated inline in both
`_resolve_model_dir()`'s `MODEL_PATH` branch and `Classifier.__init__()`'s
"caller already resolved the directory" branch, with the risk of the two
copies drifting (e.g. one handling the "no `.onnx` file yet" case and the
other not). One function, two call sites.

### `_resolve_model_dir()`

Two-path local resolution for when the DB registry is not available:

1. **`MODEL_PATH` env var** — explicit override. Returns the path, derives
   `deployed_at` via `_deployed_at_from_dir()`, and passes `source_model_id=None`
   (the HuggingFace ID is not known from just a local path).
2. **Auto-detect from `logs/optimizer/`** — scans for `report.json` files,
   parses each **exactly once** into `(report_dict, path)` pairs and sorts
   that list by `completed_at` (ISO 8601 strings sort lexicographically so
   no date parsing is needed) — an earlier version sorted by a lambda key
   that re-opened and re-parsed every file's JSON a second time just to read
   the winner back out; parsing once and keeping the parsed dict alongside
   its path avoids the redundant I/O and JSON parse for every file in
   `logs/optimizer/` on every classifier cold start. Picks the most recent,
   and reads the quantize stage's output directory and the original
   `model_id`. This is the developer convenience path so you don't need to
   copy-paste a run ID after every optimizer run.

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
- `padding=True` — pads all inputs to the **longest sequence in the batch**,
  not to a fixed length. Without padding, sequences of different lengths
  can't form a rectangular input tensor.
- `truncation=True, max_length=512` — silently truncates at 512 tokens.
  RoBERTa's positional embedding table has exactly 512 entries; longer sequences
  would index out of bounds.
- `return_tensors="np"` — returns NumPy arrays, avoiding a PyTorch allocation
  that ORT would have to convert.

### The `padding="max_length"` experiment, and why it was reverted

At one point this was changed to `padding="max_length"` — the reasoning was
that padding every batch to a fixed 512 tokens makes inference latency
independent of input length (useful for predictable p99 latency, and a
common recommendation for production ORT serving). **This was tried, and
reverted, after live testing.** With `padding="max_length"`, every batch —
including a batch of short one-sentence inputs — allocates and runs
inference on the full `(batch_size, 512)` tensor shape. Under a sustained
load test (300-trace burst through the real stream processor → classifier
path), the pod hit its 1Gi memory limit and was OOM-killed **twice**,
reproduced live, not a theoretical concern. The code now stays on
`padding=True` (pad to the batch's actual longest sequence) with a comment
explaining the revert. If fixed-shape padding is revisited later, it needs
its own verification pass — e.g. bucketed padding to a few fixed lengths
(64/128/256/512) to get *some* shape predictability without paying full 512
tokens for short inputs, or raising the pod's memory limit and re-running
the same sustained-load test to confirm it's actually stable — not a
same-session swap back without re-testing under load.

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

The problem: a single-string `/v1/moderations` call, taken on its own, would
serve one request at a time. Without batching, every concurrent single-item
request gets its own `session.run()` call, achieving the worst possible
throughput.

`DynamicBatcher` groups concurrent single-text requests into batches
automatically, with no changes required to how clients call the API. List
input skips it entirely — the caller already batched its own texts, so
`main.py` dispatches those directly instead of routing them through the queue.

### `_Pending` dataclass

```python
@dataclass
class _Pending:
    text: str
    future: asyncio.Future
```

Pairs each input text with the `asyncio.Future` that will carry its result back
to the waiting route handler. Using `asyncio.Future` (not a queue or event) lets
the route handler `await` exactly its own result — no polling, no shared state.

No dataclass default here — `submit()` always constructs the `Future` itself
via `loop.create_future()` (needs the *running* event loop, which isn't
available at class-definition time anyway) and passes it in explicitly:
`_Pending(text=text, future=loop.create_future())`. Requiring the caller to
supply it avoids the classic mutable-default-argument trap a
`field(default_factory=asyncio.Future)` default would risk — `asyncio.Future()`
constructed without a running loop binds to whatever loop happens to be
current at import/definition time, not necessarily the one actually serving
requests.

### `asyncio.Queue` — bounded, not unbounded

```python
self._queue: asyncio.Queue[_Pending] = asyncio.Queue(maxsize=settings.max_queue_depth)
```

Thread-safe within a single event loop. Route handlers (`submit()`) put items in
via `put_nowait()`; the `_loop` coroutine drains them. The queue is **bounded**
(`MAX_QUEUE_DEPTH`, default `1000`, from `config.settings`) — `put_nowait()`
raises `asyncio.QueueFull` once it's full, which `main.py`'s `_moderate_single()`
helper (the single-string branch of `/v1/moderations`) catches and turns into
an HTTP 503 ("classifier queue full — retry later") instead of accepting
unbounded work and running the pod out of memory under sustained overload.

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

### `MAX_WAIT_MS` (default `10`, `settings.max_wait_ms`)

The window for batch collection. If only one request arrives and no more come
within the window, the batcher flushes a batch of 1 rather than waiting indefinitely.
This caps the extra latency batching adds to single requests. Under high load,
the queue fills faster than the window and the deadline is never reached — `MAX_WAIT_MS`
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

`_fail_all()` is a `@staticmethod` used from two places: the `except Exception`
branch above, and the `except asyncio.CancelledError` branch below (shutdown).
Factored out because both need identical "set this exception on every
not-yet-done Future in the batch" logic.

### `stop()` — async, and it drains the queue

```python
async def stop(self) -> None:
    if self._task:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    while not self._queue.empty():
        pending = self._queue.get_nowait()
        if not pending.future.done():
            pending.future.set_exception(RuntimeError("DynamicBatcher is shutting down"))
```

Two separate failure surfaces this has to cover, both found by live-testing
pod shutdown rather than by inspection:

1. **A batch already pulled off the queue and mid-flight in `_loop()`** when
   `.cancel()` fires — `_loop()`'s `except asyncio.CancelledError:` branch
   calls `self._fail_all(batch, RuntimeError(...))` before re-raising, so
   every request already committed to that in-flight batch gets a clean
   exception instead of hanging.
2. **Requests still sitting in the queue that the loop never even picked
   up** — cancelling `self._task` doesn't touch the queue at all. Without
   the `while not self._queue.empty()` drain loop here, any request that
   arrived in the window between the last batch starting and shutdown
   beginning would sit on a `Future` that nothing will ever resolve — the
   route handler's `await pending.future` in `submit()` would hang forever,
   and (depending on how the ASGI server handles in-flight requests during
   shutdown) that could block the pod from terminating cleanly.

`stop()` had to become `async` (it wasn't originally) specifically to
`await self._task` after cancelling it — without that await, `stop()`
would return before `_loop()` actually finished unwinding, and the queue
drain below could race with `_loop()` still touching the same queue.
`main.py`'s `lifespan` shutdown sequence does `await _batcher.stop()`
before closing the DB pool for exactly this reason — see `main.py`'s
lifespan section below.

### `/v1/moderations` is `async def` and branches internally on input shape

`/v1/moderations` is declared `async def`. This might look like it
contradicts the root `CLAUDE.md`'s "classifier design rules" note about sync
routes for blocking calls — it doesn't, because neither branch inside it
calls `session.run()` directly inline. The distinction is what each branch
does:

- **Single string** (`isinstance(request.input, str)`) — `_moderate_single()`
  awaits `_batcher.submit()`, a coroutine that puts one item in the queue and
  waits for its Future. No blocking I/O directly; the actual ORT call happens
  inside `batcher.py`'s `_loop()`, itself offloaded via `run_in_executor`.
- **List** — goes through `_classify_and_persist()` (see `main.py` below),
  which calls `loop.run_in_executor(None, _classifier.predict, texts)` —
  offloads the blocking ORT call to a thread and awaits the result directly,
  since the caller already batched its own texts and there's nothing to
  coalesce with concurrent requests.

Both branches keep the event loop unblocked during inference. The key rule:
**never call a blocking C function directly in an `async def` function without
`run_in_executor`** — an `async def` route is safe as long as every blocking
call inside it is wrapped this way; it's not the `async def` itself that
would be wrong, it's calling `session.run()` unwrapped inside one.

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
VALUES ($1, $2, $3, 'staging')
ON CONFLICT (model_version) DO NOTHING
```

Called during lifespan startup, after the model is loaded — but **not
unconditionally**; see `main.py`'s lifespan section below for the
`model_dir is not None` gate that decides whether this gets called at all.
The `ON CONFLICT DO NOTHING` makes it safe to call on every pod startup — in
a multi-replica deployment, the first pod registers the version, subsequent
pods skip it silently.

**Inserts as `'staging'`, not `'active'`.** An earlier version of this
inserted `'active'` directly, on the reasoning that local dev has no
Airflow to do real promotions, so the classifier might as well self-promote.
That's what `pipelines/drift/db.py`'s `get_active_model_version()` gotcha
(see [`../../pipelines/drift/explanation.md`](../../pipelines/drift/explanation.md))
documents running into: every classifier pod self-registering as `'active'`
on every startup meant `model_registry` accumulated multiple `'active'` rows
across restarts/deploys, with no single one reliably being "the one that's
actually running." Inserting as `'staging'` here means promotion to
`'active'` is exclusively something else's job — today nothing promotes
anything (there's no automated flip yet), so `get_active_model()` above
falls back to "most recent staging row," which is honest about the current
state rather than pretending a promotion decision was made that wasn't.
Once Airflow's retrain DAG exists and does real promotions (Phase 7.3), this
staying `'staging'` is what makes that promotion step meaningful — if the
classifier kept self-promoting to `'active'`, Airflow's promotion would have
nothing distinctive to do.

### `write_classification` and `write_classifications_batch`

Two write paths for single and batch requests respectively.
`write_classifications_batch` uses `asyncpg.executemany()` — sends all rows in
a single network round-trip to PostgreSQL rather than one INSERT per row.

Both now write `span_id` and `text_type` columns (always `NULL` on this path
— these are direct API calls, not Kafka-sourced, so there's no span to
attach) and use the same conflict target as
`services/stream-processor/writer.py`'s classification writer:

```sql
ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING
```

Kept identical between the two write paths deliberately — before this, the
classifier's own writes and the stream processor's writes used slightly
different column sets, which meant neither one could be trusted to reflect
the actual schema constraint on its own. The partial index (`WHERE span_id
IS NOT NULL`) means this `ON CONFLICT` clause is a no-op for these
always-NULL-span_id rows — every direct-API classification is still
inserted every time — the idempotency guarantee only kicks in for the
stream processor's span-sourced rows.

Note: these writes are **fire-and-forget** from the route handler's perspective.
The route creates an `asyncio.Task` for the write and returns the response
immediately. If the write fails (PG down, FK constraint violation), the task logs
the exception but the route already returned 200. This is an acceptable trade-off
for low-latency inference — classification results are the primary product;
persistence is secondary. `main.py`'s `lifespan` shutdown now tracks these
tasks in a module-level `_persist_tasks` set and `await`s them before closing
the pool — see the lifespan section below for why.

When the caller sets `X-Sentinel-Skip-Persist: true` on `/v1/moderations`
(the stream processor does), these functions are not called at all — the
stream processor owns PG writes for those requests, keyed by `span_id` for
idempotency on its own side.

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

### `_s3_client()` — cached, and reads from `config.settings`

```python
@lru_cache(maxsize=1)
def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(signature_version="s3v4", connect_timeout=5, retries={"max_attempts": 2}),
        region_name="us-east-1",
    )
```

`@lru_cache(maxsize=1)` — a fresh `boto3.client("s3", ...)` per call means a
new TLS handshake and credential resolution every time; this function is
called on every `download_model()` invocation, so caching it means the
whole pod lifetime reuses one client after the first call. Deliberately
**not** shared as a common module with `pipelines/optimizer/upload.py`'s
near-identical factory function — `services/` and `pipelines/` are
separately deployed packages (different Dockerfiles, no shared workspace
member), so a real shared extraction would mean adding a new cross-package
dependency both would have to ship. Kept in sync by convention (same
`Config` args in both places) instead of by import.

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
- `endpoint`: always `"moderations"` now (`/v1/moderations` is the only classifier
  endpoint) — kept as a label rather than dropped so the metric survives if a
  second endpoint is ever added later
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

Tracks the size of each list-input `/v1/moderations` call (the external HTTP
batch, not the dynamic batcher's internal batches — single-string calls never
observe this metric). Powers-of-2 buckets match ML workload patterns where
clients tend to double their batch sizes rather than increase linearly.

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
    global _classifier, _batcher, _pool, _ready

    model_dir: Path | None = None
    active: dict | None = None

    if settings.database_url:
        try:
            _pool = await _db.init_pool(settings.database_url)
            active = await _db.get_active_model(_pool)
            if active:
                loop = asyncio.get_running_loop()
                model_dir = await loop.run_in_executor(None, download_model, active["model_path"])
        except Exception:
            logger.exception("DB init failed — running without persistence")
            if _pool is not None:
                await _pool.close()
            _pool = None
    else:
        logger.warning("DATABASE_URL not set — classifications will not be persisted")

    _classifier = Classifier(model_dir=model_dir)
    _classifier.warmup()
    _batcher = DynamicBatcher(_classifier.predict)
    _batcher.start()

    if _pool and model_dir is not None and active is not None:
        try:
            await _db.register_model(_pool, _classifier.model_version, active["model_path"], _classifier.threshold)
        except Exception:
            logger.exception("Failed to register model version in registry")
    elif _pool:
        logger.info("Skipping registry write — resolved via local fallback, not portable across pods")

    _ready = True
    yield

    _ready = False
    await _batcher.stop()
    if _pool:
        if _persist_tasks:
            await asyncio.gather(*_persist_tasks, return_exceptions=True)
        await _db.close_pool(_pool)
    _classifier = None
```

`lifespan` replaced `@app.on_event("startup")` in FastAPI 0.93+. Code before
`yield` runs at startup; code after `yield` at shutdown. The `global`
declarations give route handlers (defined at module scope) access to the
shared instances. DSN and every other tunable now come from `settings`
(`config.py`), not raw `os.environ.get(...)` calls scattered through this
function.

**`download_model` in `run_in_executor`** — boto3's `download_file` is blocking
synchronous I/O. Calling it directly in `async def lifespan` would block the
entire event loop during the download (~10–30s for the INT8 model). Running it
in the executor offloads the I/O to a thread and keeps the event loop responsive.

**The `try`/`except` around DB init, and the pool-leak fix.** If
`init_pool()` succeeds but a later call in the same block raises (e.g.
`get_active_model()` fails, or `download_model` blows up in a way that
isn't caught inside it), the `except` branch now explicitly checks
`if _pool is not None: await _pool.close()` before setting `_pool = None`.
`asyncpg.create_pool(..., min_size=1)` opens a connection eagerly at
creation time — without closing it here, that connection leaked for the
rest of the pod's lifetime on any startup failure after a successful
`init_pool()`. This was a real bug (not hypothetical): a transient DB hiccup
between pool creation and the registry query would silently leave one
connection permanently checked out, and enough restarts under a flaky DB
could exhaust PostgreSQL's `max_connections`.

**The `model_dir is not None and active is not None` registration gate —
and the bug it replaced.** This is the fix for a real production bug, found
by live-testing rather than by reading the code. The gate used to be
`_classifier.model_path.startswith("/")` (skip registration if the loaded
model's path "looks local"). That check was wrong in the *common* case, not
just an edge case: `download_model()` **always** caches the downloaded
model under a local path (`/tmp/sentinel-model-cache/...`), so
`_classifier.model_path` is *always* an absolute local path — even when the
model genuinely came from MinIO via the registry. The old check therefore
skipped registration for the normal, portable-across-pods case too. Live
symptom, confirmed via `kubectl logs`: a classifier pod loading a real
MinIO-backed model still logged "Skipping registry write," and every
subsequent `/v1/moderations` persist attempt then failed with
`psycopg.errors.ForeignKeyViolation` on `classifications_model_version_fkey`
— the model_version being written had never actually been inserted into
`model_registry`. The fix uses `model_dir is not None` instead — that
variable is only `None` when `Classifier()` fell all the way through to its
own `_resolve_model_dir()` local discovery (`MODEL_PATH` env var or
`logs/optimizer/`), which is the *actual* non-portable case. And when
registering, it passes `active["model_path"]` (the original MinIO key from
the registry row) rather than `_classifier.model_path` (the local cache
path) — the registry should record the portable MinIO reference, not a
path that only exists on this one pod's filesystem.

### `app.mount("/metrics", make_asgi_app())`

Mounts the Prometheus ASGI application at `/metrics`. `make_asgi_app()` returns
a standard ASGI app that serves all registered metrics in Prometheus text format.
Mounting it avoids running a separate HTTP server on a different port.

Prometheus scrape config uses `metrics_path: /metrics/` (trailing slash) — FastAPI
redirects `/metrics` → `/metrics/`. Specifying the final path avoids the redirect
round-trip on every scrape.

### `_classify_and_persist()` — the list-input path of `moderate()`

```python
async def _classify_and_persist(texts, endpoint, persist) -> tuple[list[dict], float, datetime]:
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
        records = [...]
        task = asyncio.create_task(_persist_batch(records))
        _persist_tasks.add(task)
        task.add_done_callback(_persist_tasks.discard)

    return results, latency_ms, inference_at
```

Handles "run inference off the event loop, record per-endpoint metrics, fire
off persistence" for `moderate()`'s list-input branch. `endpoint` is passed
through as `"moderations"` — kept as a parameter rather than hardcoded so
`REQUEST_COUNT`/`REQUEST_LATENCY` still take a label, even though today only
one caller ever passes it.

**`_persist_tasks: set[asyncio.Task]`** — every fire-and-forget persistence
task (from both the single-string and list branches of `/v1/moderations`)
is added to this module-level set and removed via
`task.add_done_callback(_persist_tasks.discard)` once it finishes. This
exists so `lifespan`'s shutdown sequence can
`await asyncio.gather(*_persist_tasks, return_exceptions=True)` before
closing the DB pool — without tracking these tasks, a classification
written in the last few requests before shutdown could still be
mid-flight when the pool closed underneath it, silently dropping that row.

### `_moderate_single()` — the single-string path of `moderate()`

```python
async def _moderate_single(text: str, persist: bool) -> dict:
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
        task = asyncio.create_task(_persist_single(text, result["label"], result["score"], _classifier.model_version, latency_ms, inference_at))
        _persist_tasks.add(task)
        task.add_done_callback(_persist_tasks.discard)

    return result
```

Mirrors `_classify_and_persist()`'s job (metrics + fire-and-forget
persistence) but for the single-item path, since that path goes through
`_batcher.submit()` instead of a direct `run_in_executor` call and returns
one `dict`, not a list — the two helpers can't share a body without adding a
branch inside the shared function, so they stay separate. Not observed on
`BATCH_SIZE` — that histogram tracks external HTTP batch sizes, and a
single-item call has no batch size of its own to report (the actual
dynamic-batcher batch it lands in is an internal implementation detail, not
something this caller chose).

### `moderate()` — the only classifier endpoint, and how it skips persistence

```python
@app.post("/v1/moderations", response_model=ModerationResponse)
async def moderate(
    request: ModerationRequest,
    x_sentinel_skip_persist: bool = Header(False, alias="X-Sentinel-Skip-Persist"),
) -> ModerationResponse:
    persist = not x_sentinel_skip_persist
    if isinstance(request.input, str):
        results = [await _moderate_single(request.input, persist)]
    else:
        results, _, _ = await _classify_and_persist(list(request.input), "moderations", persist=persist)
    return ModerationResponse(...)
```

`/classify` and `/classify/batch` were removed — nothing called them, and
this one route already covers both shapes by branching on `isinstance(request.input, str)`.
This is also the endpoint the stream processor calls — dogfooding the same
OpenAI-compatible, publicly-documented endpoint that any external
integration would use, rather than maintaining a Sentinel-internal shape as
the "real" one and an OpenAI-shaped one as a facade. See
`services/stream-processor/explanation.md` for the fuller story of that
decision and the accidental revert it survived mid-session.

**Skip-persist via header, not a body field.** The classifier's own async
PostgreSQL write needs to be skippable when the stream processor calls this
endpoint — the stream processor writes to PG itself, keyed by `span_id` for
idempotency, and would double-write otherwise. An earlier design used a
`persist: bool` field on the request body (the removed `BatchClassifyRequest`
had exactly this). That was deliberately dropped: `ModerationRequest` is
meant to be a **clean OpenAI-compatible schema** — zero Sentinel-internal
fields visible to an external caller hitting this endpoint directly (a real
`openai.moderations.create()`-style client should never need to know or care
about Sentinel's internal persistence wiring). `X-Sentinel-Skip-Persist`
moves that internal signal to a header instead, which keeps the body schema
honestly OpenAI-shaped while still letting the stream processor (an internal
caller) suppress the classifier's write.

### Environment variables summary

All of these are `config.py` `Settings` fields (env var name matches the
field name upper-cased) — see the top of this document for why they're
centralized there rather than read ad hoc in each module.

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
| `MAX_QUEUE_DEPTH` | `1000` | Max pending requests before a single-string `/v1/moderations` call returns 503 |
