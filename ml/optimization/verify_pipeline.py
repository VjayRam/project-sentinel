"""
Verify ONNX pipeline on dummy model.
Tests both CPU and GPU inference paths.
"""
import os
import time
import numpy as np
from transformers import AutoTokenizer
from optimum.onnxruntime import ORTModelForSequenceClassification
from onnxruntime.quantization import quantize_dynamic, QuantType
import onnxruntime as ort

MODEL_NAME = "VijayRam1812/content-classifier-roberta"
OUT_DIR = "../optimum_models"

print("=" * 60)
print("SENTINEL — ONNX Pipeline Verification")
print("=" * 60)

# Step 1: Export
print("\n1. Exporting to ONNX...")
model = ORTModelForSequenceClassification.from_pretrained(MODEL_NAME, export=True)
model.save_pretrained(f"{OUT_DIR}/onnx")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
tokenizer.save_pretrained(f"{OUT_DIR}/onnx")

# Step 2: Quantize
print("2. Applying INT8 dynamic quantization...")
os.makedirs(f"{OUT_DIR}/quantized", exist_ok=True)
quantize_dynamic(
    model_input=f"{OUT_DIR}/onnx/model.onnx",
    model_output=f"{OUT_DIR}/quantized/model.onnx",
    weight_type=QuantType.QInt8,
)

onnx_size = os.path.getsize(f"{OUT_DIR}/onnx/model.onnx") / 1e6
quant_size = os.path.getsize(f"{OUT_DIR}/quantized/model.onnx") / 1e6
print(f"   ONNX size:      {onnx_size:.1f} MB")
print(f"   Quantized size: {quant_size:.1f} MB")
print(f"   Reduction:      {(1 - quant_size/onnx_size)*100:.0f}%")

# Step 3: Benchmark
text = "This is a test sentence for benchmarking inference latency."
inputs = tokenizer(text, return_tensors="np", padding="max_length",
                   max_length=128, truncation=True)
ort_inputs = {k: v.astype(np.int64) for k, v in inputs.items()
              if k in ["input_ids", "attention_mask"]}

def make_session(model_path, provider):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    return ort.InferenceSession(model_path, sess_options=so, providers=[provider])

def benchmark(session, n=100):
    for _ in range(10):  # warmup
        session.run(None, ort_inputs)
    lats = []
    for _ in range(n):
        s = time.perf_counter()
        session.run(None, ort_inputs)
        lats.append((time.perf_counter() - s) * 1000)
    return np.percentile(lats, 50), np.percentile(lats, 95)

print("\n3. Benchmarking...")
results = []

# CPU — ONNX
sess = make_session(f"{OUT_DIR}/onnx/model.onnx", "CPUExecutionProvider")
p50, p95 = benchmark(sess)
results.append(("ONNX (CPU)", onnx_size, p50, p95))
print(f"   ONNX (CPU):      p50={p50:.1f}ms  p95={p95:.1f}ms")

# CPU — Quantized
sess = make_session(f"{OUT_DIR}/quantized/model.onnx", "CPUExecutionProvider")
p50, p95 = benchmark(sess)
results.append(("ONNX+INT8 (CPU)", quant_size, p50, p95))
print(f"   ONNX+INT8 (CPU): p50={p50:.1f}ms  p95={p95:.1f}ms")

# GPU — ONNX (if available)
available = ort.get_available_providers()
if "CUDAExecutionProvider" in available:
    sess = make_session(f"{OUT_DIR}/onnx/model.onnx", "CUDAExecutionProvider")
    p50, p95 = benchmark(sess)
    results.append(("ONNX (GPU)", onnx_size, p50, p95))
    print(f"   ONNX (GPU):      p50={p50:.1f}ms  p95={p95:.1f}ms")
else:
    print("   GPU: CUDAExecutionProvider not available, skipping")

# Step 4: Accuracy check
print("\n4. Verifying outputs match...")
sess_orig = make_session(f"{OUT_DIR}/onnx/model.onnx", "CPUExecutionProvider")
sess_q = make_session(f"{OUT_DIR}/quantized/model.onnx", "CPUExecutionProvider")
out_orig = sess_orig.run(None, ort_inputs)[0]
out_q = sess_q.run(None, ort_inputs)[0]
max_diff = np.max(np.abs(out_orig - out_q))
same_pred = np.argmax(out_orig) == np.argmax(out_q)
print(f"   Max logit difference: {max_diff:.6f}")
print(f"   Same prediction: {same_pred}")

# Summary table
print("\n" + "=" * 60)
print(f"{'Variant':<20} {'Size MB':>8} {'p50 ms':>8} {'p95 ms':>8}")
print("-" * 60)
for name, size, p50, p95 in results:
    print(f"{name:<20} {size:>7.1f} {p50:>7.1f} {p95:>7.1f}")
print("=" * 60)

print("\n✓ Pipeline verified. Ready to apply to your RoBERTa model.")
