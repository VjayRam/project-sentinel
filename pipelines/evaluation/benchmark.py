"""Run inference on the held-out ground-truth set and compute accuracy/F1/
AUC-ROC for a candidate model — the metrics validate.py gates promotion on.

Usage:
    uv run --package sentinel-evaluation python -m pipelines.evaluation.benchmark \\
        --model-dir logs/optimizer/<run_id>/int8 \\
        --output logs/evaluation/<run_id>/benchmark_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import resource
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

from datasets.eval_holdout import load_holdout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 32


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _score_batch(
    session: ort.InferenceSession,
    tokenizer,
    input_names: set[str],
    texts: list[str],
) -> np.ndarray:
    """Run inference and return P(harm) scores.

    Mirrors services/classifier/model.py's Classifier.predict() scoring logic
    (sigmoid for a single-logit head, softmax-last-class otherwise) — kept
    standalone here rather than imported, since pipelines/ and services/
    don't share runtime code by design (separate deployable packages, see
    CLAUDE.md's target folder structure).
    """
    inputs = tokenizer(texts, return_tensors="np", padding=True, truncation=True, max_length=512)
    ort_inputs = {k: v for k, v in inputs.items() if k in input_names}
    logits = session.run(["logits"], ort_inputs)[0]

    if logits.shape[-1] == 1:
        return _sigmoid(logits).squeeze(axis=-1)
    exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return (exp / exp.sum(axis=-1, keepdims=True))[:, -1]


def _auc_roc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """Rank-based AUC-ROC (Mann-Whitney U statistic).

    Avoids adding scikit-learn as a dependency for one metric. Ties are
    resolved by average rank via argsort-of-argsort, which is exact for
    distinct scores and a close approximation under ties — fine for a
    promotion gate, not a research benchmark.
    """
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None

    combined = np.concatenate([pos, neg])
    order = np.argsort(combined)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(combined) + 1)

    pos_rank_sum = ranks[: len(pos)].sum()
    auc = (pos_rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
    return float(auc)


def run(model_dir: str, threshold: float = 0.5, sample_size: int | None = None) -> dict:
    model_dir = Path(model_dir)
    onnx_file = next(model_dir.glob("*.onnx"), None)
    if onnx_file is None:
        raise FileNotFoundError(f"No .onnx file in {model_dir}")

    session = ort.InferenceSession(str(onnx_file), providers=["CPUExecutionProvider"])
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    input_names = {inp.name for inp in session.get_inputs()}

    holdout = load_holdout(sample_size=sample_size)
    texts = [t for t, _ in holdout]
    true_labels = np.array([1 if label == "harm" else 0 for _, label in holdout])

    logger.info("Scoring %d held-out examples | model_dir=%s", len(holdout), model_dir)
    t0 = time.perf_counter()
    scores = np.concatenate(
        [
            _score_batch(session, tokenizer, input_names, texts[i : i + BATCH_SIZE])
            for i in range(0, len(texts), BATCH_SIZE)
        ]
    )
    inference_s = time.perf_counter() - t0
    avg_latency_ms = (inference_s / len(holdout)) * 1000

    # Peak RSS for the whole process since it started (model load + inference)
    # — ru_maxrss is monotonically cumulative, not a windowed sample, so this
    # includes interpreter/library overhead too. Good enough for a promotion
    # gate ("did this candidate get dramatically heavier"), not a substitute
    # for a dedicated profiler. KB on Linux (the only target platform here;
    # macOS reports bytes, which would need /1024 instead of *1 below).
    peak_memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    pred_labels = (scores >= threshold).astype(int)

    tp = int(((pred_labels == 1) & (true_labels == 1)).sum())
    fp = int(((pred_labels == 1) & (true_labels == 0)).sum())
    fn = int(((pred_labels == 0) & (true_labels == 1)).sum())
    tn = int(((pred_labels == 0) & (true_labels == 0)).sum())

    accuracy = (tp + tn) / len(holdout)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    auc = _auc_roc(scores, true_labels)

    report = {
        "model_dir": str(model_dir),
        "n_samples": len(holdout),
        "threshold": threshold,
        "accuracy": round(accuracy, 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "auc_roc": round(auc, 4) if auc is not None else None,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "avg_latency_ms": round(avg_latency_ms, 4),
        "peak_memory_mb": round(peak_memory_mb, 2),
    }
    logger.info(
        "Benchmark complete | accuracy=%.4f precision=%.4f recall=%.4f f1=%.4f "
        "auc_roc=%s avg_latency_ms=%.2f peak_memory_mb=%.1f",
        accuracy,
        precision,
        recall,
        f1,
        report["auc_roc"],
        avg_latency_ms,
        peak_memory_mb,
    )
    return report


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark a candidate model against the held-out set")
    p.add_argument("--model-dir", required=True, help="Directory with the .onnx model + tokenizer files")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--sample-size", type=int, default=None, help="Subsample the holdout set for a quick run")
    p.add_argument("--output", default=None, help="Path to write the report as JSON")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = run(args.model_dir, args.threshold, args.sample_size)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2))
        logger.info("Report written to %s", out_path)

    print(json.dumps(result, indent=2))
