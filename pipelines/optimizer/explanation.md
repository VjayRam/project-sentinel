# Optimizer Pipeline — Component Explanations

## Directory structure

```
pipelines/optimizer/
  export.py       — Stage 1: HuggingFace Hub → ONNX FP32
  optimize.py     — Stage 2: ONNX FP32 → ONNX O2 (graph optimization)
  quantize.py     — Stage 3: ONNX O2 → ONNX INT8 (dynamic quantization)
  pipeline.py     — Orchestrator: runs all 3 stages, writes report.json
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
    ("export",   lambda: export(model_id, run_artifacts / "fp32", opset=opset)),
    ("optimize", lambda: optimize(run_artifacts / "fp32", run_artifacts / "o2")),
    ("quantize", lambda: quantize(run_artifacts / "o2", run_artifacts / "int8")),
]
```

A list of `(name, callable)` pairs instead of three separate blocks. Adding a new stage is one line. In Phase 7 when this becomes an Airflow DAG, each tuple maps directly to one `PythonOperator` and the stage name becomes the task ID.

### `report.json`

Records per-stage duration and output path for every run. This file is the contract between the optimizer and the evaluate pipeline — `benchmark.py` and `validate.py` read it to find the INT8 checkpoint without hardcoded paths. In Phase 7 it becomes the schema for what gets logged to MLflow.

### `if __name__ == "__main__"`

Makes the script both runnable directly and importable. Airflow can call `run()` as a Python function without spawning a subprocess. Running directly:

```bash
uv run python -m pipelines.optimizer.pipeline \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir models/
```

### `datetime.now(timezone.utc)`

Always UTC in timestamps — never local time. Local time is ambiguous across machines, cloud regions, and DST changes. UTC with an explicit offset (`+00:00`) is unambiguous regardless of where the pipeline runs.
