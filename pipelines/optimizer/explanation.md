# Optimizer Pipeline — Component Explanations

## Directory structure

```
pipelines/optimizer/
  export.py       — Stage 1: HuggingFace Hub → ONNX FP32
  optimize.py     — Stage 2: ONNX FP32 → ONNX O2 (graph optimization)
  quantize.py     — Stage 3: ONNX O2 → ONNX INT8 (dynamic quantization)
  upload.py       — MinIO upload for each stage's artifacts + the final report
  registry.py     — model_registry INSERT (status='staging')
  pipeline.py     — Orchestrator: runs all 3 stages, uploads, registers, writes report.json
  __main__.py     — CLI entry point (`python -m pipelines.optimizer`)
```

Each stage is a separate file because each produces a checkpoint artifact on disk. If quantization fails with a bad config, you re-run from Stage 3 without re-downloading and re-exporting the model. In Airflow (Phase 7), each stage becomes a separate task so the DAG can retry exactly the step that failed.

---

## export.py

### Why `main_export` instead of `ORTModelForSequenceClassification.from_pretrained`

The original approach used `ORTModelForSequenceClassification.from_pretrained(model_id, export=True, opset=opset)`. In optimum 2.x the `opset` parameter was removed from `from_pretrained` — passing it raises a `TypeError`. The correct API in optimum 2.x is `main_export` from `optimum.exporters.onnx`:

```python
from optimum.exporters.onnx import main_export

main_export(
    model_name_or_path=model_id,
    output=output_dir,
    task="text-classification",
    opset=opset,
)
```

`main_export` is the same function the `optimum-cli export onnx` command calls internally. It handles dynamic axes, input name mapping, and tokenizer export automatically. The `task` argument tells it which input/output signature to use — `text-classification` produces `input_ids`, `attention_mask` inputs and `logits` output.

### Opset version

The minimum recommended opset for RoBERTa in optimum 2.x is **18**, not 17. Using 17 still works but produces a warning and may miss some graph fusion opportunities. For new exports, use `opset=18`.

### Tokenizer is saved automatically

`main_export` saves the tokenizer files alongside the model, so there is no need to call `tokenizer.save_pretrained` separately. The output directory is self-contained after export.

### Output filename

`main_export` writes `model.onnx` (not `model_optimized.onnx` or `model_quantized.onnx`). The optimize stage reads `model.onnx` from the fp32 directory and writes `model_optimized.onnx` to the o2 directory.

---

## optimize.py

### `ORTOptimizer.from_pretrained(fp32_dir)`

Loads the ONNX model from the FP32 checkpoint directory. `ORTOptimizer` reads `model.onnx` and builds an internal graph representation it can rewrite.

### `OptimizationConfig(optimization_level=2, optimize_for_gpu=False, fp16=False)`

`optimization_level=2` (O2) applies two passes:

- **O1 (basic)**: constant folding, dead node elimination, redundant reshape removal
- **O2 (extended)**: transformer-specific fusions — fuses the separate Q, K, V matrix multiplies in attention into a single batched op (`Attention`), fuses residual add + LayerNorm into `SkipLayerNormalization`, fuses GeLU activation patterns

`optimize_for_gpu=False` skips GPU-specific kernel selections so the graph runs correctly on CPU. `fp16=False` keeps weights in float32 — FP16 conversion is a separate step if you ever target GPU inference.

This stage produces **zero accuracy loss** because it is purely structural: same math, fewer kernel launches, better memory layout.

### Important: O2 introduces Microsoft-domain custom ops

The fused ops (`Attention`, `SkipLayerNormalization`) live in the `com.microsoft` domain, not the standard ONNX domain. Standard ONNX shape inference cannot see through them. This has a direct consequence in the quantize stage — see below.

---

## quantize.py

### Why `quantize_dynamic` instead of `ORTQuantizer`

The original approach used Optimum's `ORTQuantizer` + `AutoQuantizationConfig`. This fails on an O2-optimized graph with:

```
RuntimeError: Unable to find data type for weight_name=
'/roberta/encoder/layer.0/attention/output/dense/MatMul_output_0'.
shape_inference failed to return a type probably this node is from a
different domain or using an input produced by such an operator.
```

The cause: the `Attention` and `SkipLayerNormalization` ops introduced by O2 are Microsoft custom ops. ORT's quantizer does type inference on every tensor in the graph. When it encounters a tensor produced by a custom-domain op, ONNX shape inference returns no type — and the quantizer crashes.

The fix is to pass `extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT}`, which tells the quantizer to assume FLOAT for any tensor whose type cannot be inferred. Optimum's `QuantizationConfig` does not expose `extra_options`, so we drop to ORT's `quantize_dynamic` directly:

```python
from onnxruntime.quantization import QuantType, quantize_dynamic
import onnx

quantize_dynamic(
    model_input=o2_dir / "model_optimized.onnx",
    model_output=output_dir / "model_quantized.onnx",
    weight_type=QuantType.QInt8,
    per_channel=False,
    extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
)
```

This is a known issue when quantizing O2-optimized transformer graphs. Dropping to the ORT API directly is the correct production solution — not a workaround.

### `weight_type=QuantType.QInt8`

Quantizes weights to signed 8-bit integers. QInt8 is the correct choice for MatMul-heavy models on CPU. QUInt8 (unsigned) is used for activations in static quantization but not here.

### `per_channel=False`

One scale factor per weight matrix (per-tensor). Per-channel uses one scale factor per output neuron — more accurate but adds overhead at inference time. For dynamic quantization on a classification model, per-tensor is the correct default.

### Dynamic quantization recap

Weights are quantized to INT8 offline and stored on disk. Activations are quantized at runtime per-inference. No calibration dataset needed. This is correct for RoBERTa because MatMul ops (linear layers) dominate compute, and quantizing weights is where most of the size and speed benefit comes from.

### Copying tokenizer files to the INT8 directory

`quantize_dynamic` only writes the `.onnx` file. The INT8 directory needs to be self-contained for the inference service to load it as a single unit. Non-model files (tokenizer, config) are copied from the O2 directory:

```python
for f in o2_dir.iterdir():
    if f.suffix in {".json", ".txt"}:
        shutil.copy2(f, output_dir / f.name)
```

### Expected results for this model

Measured on a test run of `VijayRam1812/content-classifier-roberta`:

| Checkpoint | File | Size | Stage duration |
|------------|------|------|----------------|
| FP32 ONNX  | `model.onnx` | 476 MB | 12s |
| O2         | `model_optimized.onnx` | 476 MB | 8s |
| INT8       | `model_quantized.onnx` | 120 MB | 5s |

O2 produces no size reduction (same math, different graph structure). INT8 gives a 75% size reduction. Latency benchmarks are in `pipelines/evaluation/`.

---

## upload.py

### Every stage uploads to MinIO immediately after it completes

`pipeline.py`'s stage loop calls `upload_stage(run_id, dir_name, ...)` right
after each of `export`/`optimize`/`quantize` finishes, not once at the end —
so `models/<run-id>/fp32/`, `.../o2/`, `.../int8/` land in MinIO as the
pipeline progresses, mirroring the local `models/<run-id>/` tree exactly. If
the pipeline crashes partway through (e.g. quantization OOMs), whatever
stages already completed are still durably in MinIO, not lost with the pod.

### `_s3_client()` is `@lru_cache`d

```python
@lru_cache(maxsize=1)
def _s3_client():
    return boto3.client("s3", ...)
```

A fresh `boto3.client("s3", ...)` per call means a new TLS handshake and
credential resolution every time — wasteful across the 4 upload calls one
pipeline run makes (3 stages + report). Cached so the whole run reuses one
client. Not literally shared with `services/classifier/download.py`'s
near-identical factory function — separately deployed packages by design
(see that file's own comment) — so this is a deliberate small duplication,
not an oversight.

`connect_timeout=5` + `retries={"max_attempts": 2}` on the client's `Config`
— the boto3 default connect timeout is 60 seconds; without shortening it, a
MinIO outage would hang the whole pipeline for a minute per upload attempt
instead of failing fast into the local-fallback path below.

### Parallel uploads within a stage

```python
with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
    list(pool.map(_upload_one, files))
```

The FP32 stage alone uploads the full model file plus several tokenizer
files — uploading them one at a time sequentially left the pipeline waiting
on network I/O for no reason, since the files are fully independent.
`boto3` clients are thread-safe for concurrent calls, so a `ThreadPoolExecutor`
is sufficient (no need for `asyncio`/`aioboto3`). `list(pool.map(...))`
forces the map to fully evaluate and **re-raises the first exception** it
hits — this preserves the old sequential loop's behavior of failing the
whole stage on any single file's upload error, now just running the happy
path in parallel.

### `upload_stage` and `upload_report` never raise — they return `None`

Both catch `(BotoCoreError, ClientError, OSError)` internally and log a
warning instead of propagating. This is deliberate: a MinIO outage
shouldn't take down the whole optimization run — the pipeline still
produces valid local artifacts and a local `report.json`; only the "survive
pod termination" property is lost for that run. `pipeline.py` checks the
return value (`None` = failure) to decide whether to register the MinIO
path or fall back to the local path — see `pipeline.py`'s `minio_ok` section
below.

---

## registry.py

### `register_model()` takes a connection, not a DSN

```python
def register_model(conn: psycopg.Connection, run_id: str, model_path: str, threshold: float = 0.5) -> None:
```

Earlier this opened its own `psycopg.connect()` per call. `pipeline.py`
only calls it once per run today, but the signature was changed to accept
an already-open `conn` because a pipeline run legitimately might need to
register more than once (e.g. a retry after a partial failure, or future
per-stage registration) — every `psycopg.connect()` costs a TCP handshake
plus auth round-trip, so the caller now opens **one** connection for the
whole run (`with psycopg.connect(DSN) as conn:` in `pipeline.py`) and passes
it down, rather than each callee opening its own. `DSN` was renamed from a
private `_DSN` to a public export specifically so `pipeline.py` could import
and use it for that one connection.

`ON CONFLICT (model_version) DO NOTHING` — makes registration idempotent if
the pipeline (or a retried Airflow task) calls it twice for the same
`run_id`. Status is unconditionally `'staging'`; nothing in this pipeline
ever writes `'active'` — that transition is exclusively Airflow's job per
`CLAUDE.md`'s Model Registry Source of Truth section.

---

## `__main__.py`

```bash
python -m pipelines.optimizer --model-id VijayRam1812/content-classifier-roberta --output-dir models/
```

A dedicated `__main__.py` rather than the `if __name__ == "__main__":` block
living in `pipeline.py` itself — lets the module be run as
`python -m pipelines.optimizer` (the package) instead of
`python -m pipelines.optimizer.pipeline` (a specific file inside it). This
is the more standard invocation for a package meant to be run as a unit —
matches how `python -m pytest`, `python -m http.server`, etc. work — and
keeps `pipeline.py` purely a library module (importable by Airflow as a
plain `run()` function call, with argument parsing living only at the CLI
boundary).

---

## pipeline.py

### Run ID

```python
run_id = str(uuid.uuid4())
```

Every pipeline invocation gets a UUID4 as its identity. UUID4 is fully random — no coordination with a database or counter needed to guarantee uniqueness. This is the same mechanism MLflow uses for run IDs, and it is what you would use as a primary key if you stored runs in PostgreSQL. The full UUID is used as the folder name; log messages attach `run_id=` to every line so you can grep a specific run out of aggregated logs.

### Separated artifact and log paths

```python
run_artifacts = Path(output_dir) / run_id   # model checkpoints
run_log       = Path(log_dir) / "optimizer" / run_id  # report.json
```

Artifacts (fp32, o2, int8 model checkpoints) and logs (report.json) live in different directory trees intentionally:

- `models/<run-id>/` holds large binary files. These are gitignored and will eventually move to MinIO/S3.
- `logs/optimizer/<run-id>/` holds small JSON metadata. This is also gitignored locally but is the data source for dashboards, MLflow, and audit trails in production.

The `log_dir` defaults to `logs/` but is a CLI argument so CI or Airflow can redirect it without changing code (e.g. to a mounted volume or a shared network path).

The resulting layout after a run:

```
models/
  <run-id>/
    fp32/       ← model.onnx + tokenizer files
    o2/         ← model_optimized.onnx + tokenizer files
    int8/       ← model_quantized.onnx + tokenizer files

logs/
  optimizer/
    <run-id>/
      report.json
```

### `run_id` in the report

```python
report: dict = {
    "run_id": run_id,
    ...
}
```

The report carries its own `run_id` so the file is self-describing — you can move or copy `report.json` without losing the link back to the artifacts directory. In Phase 7, this field becomes the primary key when the report is logged to MLflow.

### `run_id` in log messages

```python
logger.info("--- Stage: %s | run_id=%s ---", name, run_id)
```

Structured log fields (`key=value`) let log aggregators (Datadog, Loki, CloudWatch) filter and group by `run_id` without parsing free-form text. This is a habit worth building from the start — it costs nothing to add and saves significant debugging time when logs from concurrent runs are interleaved.

### Logging configured only at the entry point

```python
logging.basicConfig(level=logging.INFO, format="...")
```

Logging is configured here and nowhere else. The stage modules use `logging.getLogger(__name__)` and emit records without configuring handlers. If `basicConfig` were called inside `export.py`, it would fight with whatever the caller configured. Entry point owns the handler; modules own the logger.

### Stage list pattern

```python
stages = [
    ("export",   lambda: export(model_id, run_artifacts / "fp32", opset=opset), "fp32"),
    ("optimize", lambda: optimize(run_artifacts / "fp32", run_artifacts / "o2"), "o2"),
    ("quantize", lambda: quantize(run_artifacts / "o2", run_artifacts / "int8"), "int8"),
]
```

A list of `(name, callable, dir_name)` triples instead of three separate
blocks — the third element (`dir_name`) doubles as both the local
subdirectory the stage writes to and the MinIO key prefix
(`upload_stage(run_id, dir_name, ...)`), so the bucket layout mirrors the
local `models/<run-id>/` tree exactly without a second naming scheme to keep
in sync. Adding a new stage is one line. In Phase 7 when this becomes an
Airflow DAG, each triple maps directly to one `PythonOperator`/
`KubernetesPodOperator` and the stage name becomes the task ID.

### MinIO upload + registration, and the local-fallback path

After each stage's callable runs, `upload_stage()` is called immediately
(see `upload.py` above) and its return value is folded into
`report["stages"][name]["minio_path"]`. A module-level `minio_ok` flag
starts `True` and flips to `False` the first time any stage's upload
returns `None` (MinIO unreachable).

After all three stages complete, `model_path` — the value that ends up in
`model_registry.model_path`, and what the classifier's `download.py`
actually fetches on pod startup — is derived like this:

```python
if minio_ok:
    model_path = f"{report['stages']['quantize']['minio_path']}/model_quantized.onnx"
else:
    model_path = str(run_artifacts / "int8")  # local fallback
```

Deliberately built from the quantize stage's **actual** `upload_stage()`
return value rather than re-deriving `f"models/{run_id}/int8"` by hand — an
earlier version hardcoded the `"models"` bucket-name prefix here a second
time, which risked silently diverging from `upload.py`'s real
`MINIO_BUCKET` env var if that were ever set to something other than
`"models"` (bug tracked as #35). Reading it back out of the report the
upload step already wrote means there's exactly one place the bucket name
is resolved.

The local-fallback branch means a MinIO outage doesn't stop `register_model`
from recording that *a* run happened — the registry entry just won't be
resolvable by any other pod (only the machine that ran the pipeline has that
local path), so it's logged with a warning, not silently treated as normal.

### `report.json`

Records per-stage duration, output path, and MinIO path for every run,
plus a top-level `model_path` mirroring whatever was actually registered.
This file is the contract between the optimizer and the evaluation
pipeline — `benchmark.py`/`validate.py` read it (indirectly, via
`--model-dir`) to find the INT8 checkpoint without hardcoded paths. It's
also uploaded to MinIO itself (`upload_report()`) after being written
locally, so it survives pod termination the same way the model artifacts
do. In Phase 7 it becomes the schema for what gets logged to MLflow.

### Kept importable, not just runnable

`pipeline.py` itself has no `if __name__ == "__main__":` block anymore — CLI
parsing lives entirely in `__main__.py` (see above). `run()` stays a plain
function so Airflow can call it directly as a Python callable without
spawning a subprocess. Running directly:

```bash
uv run python -m pipelines.optimizer \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir models/
```

### `datetime.now(timezone.utc)`

Always UTC in timestamps — never local time. Local time is ambiguous across machines, cloud regions, and DST changes. UTC with an explicit offset (`+00:00`) is unambiguous regardless of where the pipeline runs.
