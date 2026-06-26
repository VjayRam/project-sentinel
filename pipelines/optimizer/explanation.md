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

### `ORTModelForSequenceClassification`

Optimum's wrapper around HuggingFace's model class. Using this instead of raw `AutoModelForSequenceClassification` gives you the `export=True` flag and ONNX-aware save behavior. With the vanilla HF class you would have to call `torch.onnx.export()` manually and handle dynamic axes, opsets, and input names yourself.

### `from_pretrained(model_id, export=True, opset=opset)`

`export=True` tells Optimum to run ONNX conversion during `from_pretrained` rather than loading an already-converted model. Internally it traces the model through a dummy forward pass, captures the compute graph, and serializes it to `.onnx`. The `opset` controls which version of the ONNX operator spec to target — higher opsets unlock more graph fusion patterns in the optimize step. Opset 17 is the current stable ceiling.

### Saving the tokenizer alongside the model

```python
tokenizer = AutoTokenizer.from_pretrained(model_id)
tokenizer.save_pretrained(output_dir)
```

The ONNX model file alone is useless without knowing how to tokenize input text. Saving the tokenizer into the same directory as the model means every checkpoint directory is self-contained — the inference service loads one directory and gets everything it needs.

---

## optimize.py

### `ORTOptimizer.from_pretrained(fp32_dir)`

Loads the ONNX model from the FP32 checkpoint directory. `ORTOptimizer` reads the `.onnx` file and builds an internal graph representation it can rewrite.

### `OptimizationConfig(optimization_level=2, optimize_for_gpu=False, fp16=False)`

`optimization_level=2` (O2) applies two passes:

- **O1 (basic)**: constant folding, dead node elimination, redundant reshape removal
- **O2 (extended)**: transformer-specific fusions — fuses the separate Q, K, V matrix multiplies in attention into a single batched op, fuses LayerNorm into a single kernel, fuses GeLU activation patterns

`optimize_for_gpu=False` skips GPU-specific kernel selections so the graph runs correctly on CPU. `fp16=False` keeps weights in float32 — FP16 conversion is a separate step if you ever target GPU inference.

This stage produces **zero accuracy loss** because it is purely structural: same math, fewer kernel launches, better memory layout.

---

## quantize.py

### `ORTQuantizer.from_pretrained(o2_dir)`

Loads from the O2 checkpoint, not the FP32 one. You always quantize the already-optimized graph. Quantizing the raw export and then optimizing gives worse results because some fusions do not fire on INT8 graphs.

### `AutoQuantizationConfig.avx2(is_static=False, per_channel=False)`

`avx2` selects the INT8 operator kernels for CPUs with AVX2 SIMD instructions (any Intel/AMD CPU since ~2013). On an AVX-512 server you would use `avx512_vnni` instead for another ~20% speedup.

`is_static=False` means **dynamic quantization**: weights are quantized to INT8 offline and stored on disk. Activations (intermediate tensors during inference) are quantized at runtime per-batch. No calibration dataset is needed. This is the right default for transformer text classifiers because MatMul ops dominate compute, and quantizing weights is where most of the size and speed benefit comes from.

`per_channel=False` uses one scale factor per weight matrix (per-tensor). Per-channel would use one scale factor per output neuron — more accurate but slower to apply at inference time. For dynamic quantization on a classification model, per-tensor is the correct default.

### Expected results for a RoBERTa-base model

| Checkpoint | Size    | p50 latency | Accuracy loss vs FP32 |
|------------|---------|-------------|----------------------|
| FP32 ONNX  | ~480 MB | ~110ms      | baseline             |
| O2         | ~480 MB | ~60ms       | 0%                   |
| INT8       | ~120 MB | ~35ms       | <0.2%                |

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
    fp32/       ← ONNX FP32 checkpoint + tokenizer
    o2/         ← ONNX O2 checkpoint + tokenizer
    int8/       ← ONNX INT8 checkpoint + tokenizer

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

Structured log fields (`key=value` at the end of each message) let log aggregators (Datadog, Loki, CloudWatch) filter and group by `run_id` without parsing free-form text. This is a habit worth building from the start — it costs nothing to add and saves significant debugging time when logs from concurrent runs are interleaved.

### Logging configured only at the entry point

```python
logging.basicConfig(level=logging.INFO, format="...")
```

Logging is configured here and nowhere else. The stage modules use `logging.getLogger(__name__)` and emit records without configuring handlers. If `basicConfig` were called inside `export.py`, it would fight with whatever the caller configured. Entry point owns the handler; modules own the logger.

### Stage list pattern

```python
stages = [
    ("export",   lambda: export(model_id, base / "fp32", opset=opset)),
    ("optimize", lambda: optimize(base / "fp32", base / "o2")),
    ("quantize", lambda: quantize(base / "o2", base / "int8")),
]
```

A list of `(name, callable)` pairs instead of three separate blocks. Adding a new stage is one line. In Phase 7 when this becomes an Airflow DAG, each tuple maps directly to one `PythonOperator` and the stage name becomes the task ID.

### `report.json`

Records per-stage duration and output path for every run. This file is the contract between the optimizer and the evaluate pipeline — `benchmark.py` and `validate.py` read it to find the INT8 checkpoint without hardcoded paths. In Phase 7 it becomes the schema for what gets logged to MLflow. Define it once here so you do not have to reverse-engineer it later.

### `if __name__ == "__main__"`

Makes the script both runnable directly and importable. Airflow can call `run()` as a Python function without spawning a subprocess. Running directly:

```bash
uv run python -m pipelines.optimizer.pipeline \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir models/
```

### `datetime.now(timezone.utc)`

Always UTC in timestamps — never local time. Local time is ambiguous across machines, cloud regions, and DST changes. The report will be read by tools and people in different timezones; UTC with an explicit offset (`+00:00`) is unambiguous.
