"""
Quick toolchain verification using a small public model.
Does NOT use the real RoBERTa model — use optimize.py for that.

Checks: ONNX export → INT8 quantization → CPU/GPU benchmark → logit diff

Run: python ml/optimization/verify_pipeline.py  (~2 min)
"""

import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from onnxruntime.quantization import QuantType, quantize_dynamic
from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoTokenizer

DUMMY_MODEL = "distilbert-base-uncased-finetuned-sst-2-english"
OUT_DIR = Path("/tmp/sentinel_verify")
MAX_LENGTH = 128

print("=" * 60)
print("  SENTINEL — Pipeline Toolchain Verification")
print(f"  Dummy model: {DUMMY_MODEL}")
print("=" * 60)


def make_session(
    model_path: str, provider: str = "CPUExecutionProvider"
) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    return ort.InferenceSession(model_path, sess_options=so, providers=[provider])


def benchmark(
    session: ort.InferenceSession, inputs: dict, n: int = 100
) -> tuple[float, float]:
    for _ in range(10):
        session.run(None, inputs)
    latencies = []
    for _ in range(n):
        t0 = time.perf_counter()
        session.run(None, inputs)
        latencies.append((time.perf_counter() - t0) * 1000)
    return float(np.percentile(latencies, 50)), float(np.percentile(latencies, 95))


OUT_DIR.mkdir(parents=True, exist_ok=True)
onnx_dir = OUT_DIR / "onnx"
quant_dir = OUT_DIR / "quantized"
quant_dir.mkdir(parents=True, exist_ok=True)

# 1. Export
print("\n1. Exporting to ONNX...")
model = ORTModelForSequenceClassification.from_pretrained(DUMMY_MODEL, export=True)
model.save_pretrained(onnx_dir)
tokenizer = AutoTokenizer.from_pretrained(DUMMY_MODEL)
print(f"   Saved → {onnx_dir}")

# 2. Quantize
print("2. Applying INT8 dynamic quantization...")
onnx_path = onnx_dir / "model.onnx"
quant_path = quant_dir / "model.onnx"
quantize_dynamic(
    model_input=str(onnx_path),
    model_output=str(quant_path),
    weight_type=QuantType.QInt8,
    extra_options={"MatMulConstBOnly": True},
)
size_onnx = os.path.getsize(onnx_path) / 1e6
size_quant = os.path.getsize(quant_path) / 1e6
print(f"   ONNX FP32:  {size_onnx:.1f} MB")
print(
    f"   ONNX INT8:  {size_quant:.1f} MB  ({(1 - size_quant / size_onnx) * 100:.0f}% reduction)"
)

# 3. Benchmark
text = "This is a test sentence for benchmarking inference latency."
enc = tokenizer(
    text,
    return_tensors="np",
    padding="max_length",
    max_length=MAX_LENGTH,
    truncation=True,
)
ort_inputs = {
    k: v.astype(np.int64)
    for k, v in enc.items()
    if k in ["input_ids", "attention_mask"]
}

print("3. Benchmarking...")
results = []

sess = make_session(str(onnx_path))
p50, p95 = benchmark(sess, ort_inputs)
results.append(("ONNX FP32 (CPU)", size_onnx, p50, p95))
print(f"   ONNX FP32 (CPU):  p50={p50:.1f}ms  p95={p95:.1f}ms")

sess_q = make_session(str(quant_path))
p50, p95 = benchmark(sess_q, ort_inputs)
results.append(("ONNX INT8 (CPU)", size_quant, p50, p95))
print(f"   ONNX INT8 (CPU):  p50={p50:.1f}ms  p95={p95:.1f}ms")

if "CUDAExecutionProvider" in ort.get_available_providers():
    sess_gpu = make_session(str(onnx_path), "CUDAExecutionProvider")
    p50, p95 = benchmark(sess_gpu, ort_inputs)
    results.append(("ONNX FP32 (GPU)", size_onnx, p50, p95))
    print(f"   ONNX FP32 (GPU):  p50={p50:.1f}ms  p95={p95:.1f}ms")
else:
    print("   GPU: CUDAExecutionProvider not available — skipping")

# 4. Accuracy check
print("4. Verifying logit drift...")
out_fp32 = make_session(str(onnx_path)).run(None, ort_inputs)[0]
out_int8 = make_session(str(quant_path)).run(None, ort_inputs)[0]
max_diff = float(np.max(np.abs(out_fp32 - out_int8)))
same_pred = np.argmax(out_fp32) == np.argmax(out_int8)
print(f"   Max logit diff:  {max_diff:.6f}")
print(f"   Same prediction: {same_pred}")

# Summary
print("\n" + "=" * 60)
print(f"{'Variant':<20} {'Size MB':>8} {'p50 ms':>8} {'p95 ms':>8}")
print("-" * 60)
for name, size, p50, p95 in results:
    print(f"{name:<20} {size:>8.1f} {p50:>8.1f} {p95:>8.1f}")
print("=" * 60)
print("\n  Toolchain verified. Run optimize.py for the real model.")
