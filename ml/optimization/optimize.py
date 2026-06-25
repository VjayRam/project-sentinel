"""
Optimize VijayRam1812/content-classifier-roberta for production inference.

Steps:
  0. PyTorch FP32 baseline — latency + accuracy on test dataset
  1. ONNX export           — mathematically identical, operator fusion via ORT
  2. O2 graph optimization — transformer-specific fusions (attention, layer norm)
  3. INT8 dynamic quant    — weights quantized to INT8, ~75% size reduction

Benchmarking uses data/benchmark/test_dataset.csv (3780 samples, real distribution).
Each variant is evaluated for latency (p50/p95/p99 across all samples) and accuracy
(accuracy, F1, AUC-ROC against ground-truth labels).

Output:
  models/tokenizer/        shared tokenizer
  models/onnx/             ONNX FP32
  models/onnx_optimized/   ONNX + O2
  models/onnx_quantized/   ONNX + O2 + INT8  ← deploy this
  models/benchmark.json    full results

Run from project root:
  python ml/optimization/optimize.py
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pandas as pd
import torch
from onnxruntime.quantization import QuantType, quantize_dynamic
from optimum.onnxruntime import ORTModelForSequenceClassification, ORTOptimizer
from optimum.onnxruntime.configuration import OptimizationConfig
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from transformers import AutoModelForSequenceClassification, AutoTokenizer

try:
    import psycopg2
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

MODEL_HF   = "VijayRam1812/content-classifier-roberta"
ROOT_DIR   = Path(__file__).parent.parent.parent
MODELS_DIR = ROOT_DIR / "models"
DATA_PATH  = ROOT_DIR / "data" / "benchmark" / "test_dataset.csv"
MAX_LENGTH = 512
WARMUP_N   = 20   # samples used for ORT session warmup before timing

# ── helpers ──────────────────────────────────────────────────────────────────

def make_ort_session(model_path: str, provider: str = "CPUExecutionProvider") -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 1
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    so.enable_mem_pattern = True
    so.enable_cpu_mem_arena = True
    return ort.InferenceSession(model_path, sess_options=so, providers=[provider])


def run_ort_dataset(
    session: ort.InferenceSession,
    encoded: list[dict],
    warmup: int = WARMUP_N,
) -> tuple[list[float], list[int], list[float]]:
    """
    Run the full dataset through an ORT session.
    Returns (latencies_ms, predicted_labels, harmful_probs).
    First `warmup` samples are used to warm the session and excluded from timing.
    """
    for inp in encoded[:warmup]:
        session.run(None, inp)

    latencies, preds, probs = [], [], []
    for inp in encoded:
        t0 = time.perf_counter()
        logits = session.run(None, inp)[0][0]
        latencies.append((time.perf_counter() - t0) * 1000)
        p = _softmax(logits)
        preds.append(int(np.argmax(p)))
        probs.append(float(p[1]))

    return latencies, preds, probs


def run_pytorch_dataset(
    model,
    encoded_pt: list[dict],
    warmup: int = WARMUP_N,
) -> tuple[list[float], list[int], list[float]]:
    model.eval()
    for inp in encoded_pt[:warmup]:
        with torch.no_grad():
            model(**inp)

    latencies, preds, probs = [], [], []
    with torch.no_grad():
        for inp in encoded_pt:
            t0 = time.perf_counter()
            out = model(**inp)
            latencies.append((time.perf_counter() - t0) * 1000)
            p = torch.softmax(out.logits[0], dim=0).numpy()
            preds.append(int(np.argmax(p)))
            probs.append(float(p[1]))

    return latencies, preds, probs


def latency_stats(latencies: list[float]) -> tuple[float, float, float]:
    a = np.array(latencies)
    return float(np.percentile(a, 50)), float(np.percentile(a, 95)), float(np.percentile(a, 99))


def accuracy_metrics(labels: list[int], preds: list[int], probs: list[float]) -> dict:
    return {
        "accuracy": round(accuracy_score(labels, preds), 4),
        "f1":       round(f1_score(labels, preds), 4),
        "auc_roc":  round(roc_auc_score(labels, probs), 4),
    }


def model_size_mb(path) -> float:
    return os.path.getsize(path) / 1e6


def _softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - np.max(logits))
    return e / e.sum()


def _model_version() -> str:
    """Generate a collision-resistant version string: v{YYYYMMDD}-{git-sha}."""
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        sha = "local"
    return f"v{date}-{sha}"


def section(title: str) -> None:
    print(f"\n{'─' * 65}")
    print(f"  {title}")
    print(f"{'─' * 65}")


def register_model(version: str, onnx_path: str, result: dict) -> None:
    """Write benchmark results to model_registry. Skips silently if DB is unreachable."""
    if not _PSYCOPG2_AVAILABLE:
        print("  psycopg2 not available — skipping registry write")
        return
    db_url = os.getenv("SENTINEL_DB_URL")
    if not db_url:
        print("  SENTINEL_DB_URL not set — skipping registry write")
        return
    try:
        conn = psycopg2.connect(db_url)
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO model_registry
                    (version, onnx_path, accuracy, f1, auc_roc,
                     size_mb, p50_latency_ms, p95_latency_ms, p99_latency_ms,
                     trained_at, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'staging')
                ON CONFLICT (version) DO UPDATE SET
                    onnx_path      = EXCLUDED.onnx_path,
                    accuracy       = EXCLUDED.accuracy,
                    f1             = EXCLUDED.f1,
                    auc_roc        = EXCLUDED.auc_roc,
                    size_mb        = EXCLUDED.size_mb,
                    p50_latency_ms = EXCLUDED.p50_latency_ms,
                    p95_latency_ms = EXCLUDED.p95_latency_ms,
                    p99_latency_ms = EXCLUDED.p99_latency_ms,
                    trained_at     = NOW()
                """,
                (
                    version, onnx_path,
                    result.get("accuracy"), result.get("f1"), result.get("auc_roc"),
                    result.get("size_mb"),
                    result.get("p50_ms"), result.get("p95_ms"), result.get("p99_ms"),
                ),
            )
        conn.close()
        print(f"  Registered {version} in model_registry (status=staging)")
    except Exception as exc:
        print(f"  Registry write failed: {exc}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 65)
    print("  SENTINEL — RoBERTa Optimization")
    print(f"  Model:   {MODEL_HF}")
    print(f"  Dataset: {DATA_PATH}")
    print("=" * 65)

    # ── load dataset ─────────────────────────────────────────────────────────
    section("Loading dataset")
    df = pd.read_csv(DATA_PATH)
    texts  = df["raw_text"].tolist()
    labels = df["label"].astype(int).tolist()
    print(f"  Rows:    {len(df)}")
    print(f"  Label 0 (safe):    {labels.count(0)}")
    print(f"  Label 1 (harmful): {labels.count(1)}")

    # ── create output dirs ───────────────────────────────────────────────────
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tokenizer_dir = MODELS_DIR / "tokenizer"
    onnx_dir      = MODELS_DIR / "onnx"
    opt_dir       = MODELS_DIR / "onnx_optimized"
    quant_dir     = MODELS_DIR / "onnx_quantized"
    for d in [tokenizer_dir, onnx_dir, opt_dir, quant_dir]:
        d.mkdir(parents=True, exist_ok=True)

    results = []

    # ── tokenizer + pre-tokenize full dataset ────────────────────────────────
    section("Tokenizing dataset")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_HF)
    tokenizer.save_pretrained(tokenizer_dir)

    print(f"  Tokenizing {len(texts)} samples at max_length={MAX_LENGTH}...")
    encoded_ort = []
    encoded_pt  = []
    for text in texts:
        enc = tokenizer(
            text,
            return_tensors="np",
            truncation=True,
            max_length=MAX_LENGTH,
            padding="max_length",
        )
        ort_inp = {k: enc[k].astype(np.int64) for k in ["input_ids", "attention_mask"]}
        encoded_ort.append(ort_inp)
        encoded_pt.append({k: torch.tensor(v) for k, v in ort_inp.items()})

    print(f"  Done. Tokenizer saved → {tokenizer_dir}")

    # ── step 0: PyTorch FP32 baseline ────────────────────────────────────────
    section("Step 0 — PyTorch FP32 baseline")
    pt_model = AutoModelForSequenceClassification.from_pretrained(MODEL_HF)
    pt_model.eval()
    id2label = pt_model.config.id2label
    print(f"  Labels: {id2label}")

    pt_size_mb = sum(p.numel() * p.element_size() for p in pt_model.parameters()) / 1e6
    print(f"  Size: {pt_size_mb:.1f} MB (in-memory parameters)")
    print(f"  Running inference on {len(texts)} samples...")
    lats, preds, probs = run_pytorch_dataset(pt_model, encoded_pt)
    p50, p95, p99 = latency_stats(lats)
    metrics = accuracy_metrics(labels, preds, probs)
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
    print(f"  Accuracy={metrics['accuracy']}  F1={metrics['f1']}  AUC-ROC={metrics['auc_roc']}")
    results.append({"variant": "PyTorch FP32", "size_mb": round(pt_size_mb, 1),
                    "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
                    **metrics})
    baseline_preds = preds
    del pt_model

    # ── step 1: ONNX export ──────────────────────────────────────────────────
    section("Step 1 — ONNX export")
    ort_model = ORTModelForSequenceClassification.from_pretrained(MODEL_HF, export=True)
    ort_model.save_pretrained(onnx_dir)
    onnx_path = onnx_dir / "model.onnx"
    size_onnx = model_size_mb(onnx_path)
    print(f"  Size: {size_onnx:.1f} MB  →  {onnx_dir}")
    print(f"  Running inference on {len(texts)} samples...")
    sess = make_ort_session(str(onnx_path))
    lats, preds, probs = run_ort_dataset(sess, encoded_ort)
    p50, p95, p99 = latency_stats(lats)
    metrics = accuracy_metrics(labels, preds, probs)
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
    print(f"  Accuracy={metrics['accuracy']}  F1={metrics['f1']}  AUC-ROC={metrics['auc_roc']}")
    agreement = sum(a == b for a, b in zip(baseline_preds, preds)) / len(preds)
    print(f"  Prediction agreement with PyTorch FP32: {agreement:.4f}")
    results.append({"variant": "ONNX FP32", "size_mb": round(size_onnx, 1),
                    "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
                    **metrics})

    # ── step 2: O2 graph optimization ────────────────────────────────────────
    section("Step 2 — O2 graph optimization")
    optimizer  = ORTOptimizer.from_pretrained(ort_model)
    opt_config = OptimizationConfig(
        optimization_level=2,
        enable_transformers_specific_optimizations=True,
        optimize_for_gpu=False,
    )
    optimizer.optimize(save_dir=str(opt_dir), optimization_config=opt_config)
    opt_path = opt_dir / "model_optimized.onnx"
    if not opt_path.exists():
        opt_path = opt_dir / "model.onnx"
    size_opt = model_size_mb(opt_path)
    print(f"  Size: {size_opt:.1f} MB  →  {opt_dir}")
    print(f"  Running inference on {len(texts)} samples...")
    sess_opt = make_ort_session(str(opt_path))
    lats, preds, probs = run_ort_dataset(sess_opt, encoded_ort)
    p50, p95, p99 = latency_stats(lats)
    metrics = accuracy_metrics(labels, preds, probs)
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
    print(f"  Accuracy={metrics['accuracy']}  F1={metrics['f1']}  AUC-ROC={metrics['auc_roc']}")
    results.append({"variant": "ONNX O2", "size_mb": round(size_opt, 1),
                    "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
                    **metrics})

    # ── step 3: INT8 dynamic quantization ────────────────────────────────────
    section("Step 3 — INT8 dynamic quantization")
    quant_path = quant_dir / "model.onnx"
    quantize_dynamic(
        model_input=str(opt_path),
        model_output=str(quant_path),
        weight_type=QuantType.QInt8,
        extra_options={"MatMulConstBOnly": True},
    )
    tokenizer.save_pretrained(quant_dir)   # self-contained for the classifier service
    size_quant = model_size_mb(quant_path)
    print(f"  Size: {size_quant:.1f} MB  →  {quant_dir}")
    print(f"  Running inference on {len(texts)} samples...")
    sess_q = make_ort_session(str(quant_path))
    lats, preds, probs = run_ort_dataset(sess_q, encoded_ort)
    p50, p95, p99 = latency_stats(lats)
    metrics = accuracy_metrics(labels, preds, probs)
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")
    print(f"  Accuracy={metrics['accuracy']}  F1={metrics['f1']}  AUC-ROC={metrics['auc_roc']}")
    agreement = sum(a == b for a, b in zip(baseline_preds, preds)) / len(preds)
    print(f"  Prediction agreement with PyTorch FP32: {agreement:.4f}")
    results.append({"variant": "ONNX O2 + INT8", "size_mb": round(size_quant, 1),
                    "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
                    **metrics})

    # ── GPU (if available) ───────────────────────────────────────────────────
    if "CUDAExecutionProvider" in ort.get_available_providers():
        section("GPU benchmark (CUDAExecutionProvider)")
        for variant, path in [("ONNX FP32 (GPU)", onnx_path), ("ONNX O2+INT8 (GPU)", quant_path)]:
            sess_gpu = make_ort_session(str(path), "CUDAExecutionProvider")
            lats, preds, probs = run_ort_dataset(sess_gpu, encoded_ort)
            p50, p95, p99 = latency_stats(lats)
            metrics = accuracy_metrics(labels, preds, probs)
            size = model_size_mb(path)
            print(f"  {variant}: p50={p50:.1f}ms  p95={p95:.1f}ms  Acc={metrics['accuracy']}")
            results.append({"variant": variant, "size_mb": round(size, 1),
                            "p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
                            **metrics})
    else:
        print("\n  GPU: CUDAExecutionProvider not available — skipping")

    # ── results table ────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"{'Variant':<22} {'MB':>6} {'p50':>6} {'p95':>6} {'p99':>6} {'Acc':>7} {'F1':>7} {'AUC':>7}")
    print("-" * 80)
    for r in results:
        print(
            f"{r['variant']:<22} {r['size_mb']:>6.1f} {r['p50_ms']:>6.1f} "
            f"{r['p95_ms']:>6.1f} {r['p99_ms']:>6.1f} "
            f"{r.get('accuracy', 0):>7.4f} {r.get('f1', 0):>7.4f} {r.get('auc_roc', 0):>7.4f}"
        )
    if len(results) >= 4:
        b, f = results[0], results[3]
        print("-" * 80)
        print(f"  Size reduction  (FP32 → INT8): {(1 - f['size_mb'] / b['size_mb']) * 100:.0f}%")
        print(f"  Latency p50 reduction:          {(1 - f['p50_ms'] / b['p50_ms']) * 100:.0f}%")
        print(f"  Accuracy delta:                 {f.get('accuracy', 0) - b.get('accuracy', 0):+.4f}")
    print("=" * 80)

    # ── save report ──────────────────────────────────────────────────────────
    report_path = MODELS_DIR / "benchmark.json"
    with open(report_path, "w") as fh:
        json.dump({
            "model":        MODEL_HF,
            "dataset":      str(DATA_PATH),
            "n_samples":    len(texts),
            "max_length":   MAX_LENGTH,
            "results":      results,
        }, fh, indent=2)
    print(f"\n  Report → {report_path}")
    print(f"  Deploy → {quant_dir}/model.onnx")

    # ── write to model_registry ───────────────────────────────────────────────
    # Uses the INT8 result (index 3). Requires SENTINEL_DB_URL env var.
    # Example: SENTINEL_DB_URL="postgresql://sentinel:sentinel@localhost:5432/sentinel"
    int8_result = next((r for r in results if r["variant"] == "ONNX O2 + INT8"), None)
    if int8_result:
        register_model(_model_version(), str(quant_path), int8_result)

    print("\n  Done.")


if __name__ == "__main__":
    main()
