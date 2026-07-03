import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort
from config import settings
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "uv.lock").exists():
            return candidate
    return here


def _deployed_at_from_dir(model_dir: Path) -> str:
    """Derive a deployed_at timestamp string from a model directory's .onnx
    file mtime, falling back to now() if no .onnx file is present yet.
    """
    onnx_file = next(model_dir.glob("*.onnx"), None)
    mtime = onnx_file.stat().st_mtime if onnx_file else None
    ts = datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else datetime.now(timezone.utc)
    return ts.strftime("%Y%m%dT%H%M%SZ")


def _resolve_model_dir() -> tuple[Path, str, str | None]:
    """Returns (model_dir, deployed_at, source_model_id).

    source_model_id is the HuggingFace model ID from the optimizer report, or
    None when the path is supplied explicitly via MODEL_PATH.
    """
    if settings.model_path:
        p = Path(settings.model_path)
        return p, _deployed_at_from_dir(p), None

    log_root = _project_root() / "logs" / "optimizer"
    if not log_root.exists():
        raise RuntimeError("No MODEL_PATH set and logs/optimizer/ not found")

    # Parse each report.json exactly once — sort the (report, path) pairs
    # themselves rather than re-parsing the winner after sorting by a lambda
    # key that already parsed every file once.
    reports = [(json.loads(p.read_text()), p) for p in log_root.glob("*/report.json")]
    reports.sort(key=lambda item: item[0].get("completed_at", ""), reverse=True)
    if not reports:
        raise RuntimeError("No completed optimization runs found in logs/optimizer/")

    report, _ = reports[0]
    model_dir = _project_root() / report["stages"]["quantize"]["output"]
    logger.info("Auto-selected model from run %s", report["run_id"])

    try:
        ts = datetime.fromisoformat(report["completed_at"])
        deployed_at = ts.strftime("%Y%m%dT%H%M%SZ")
    except (KeyError, ValueError):
        deployed_at = "unknown"

    return model_dir, deployed_at, report.get("model_id")


class Classifier:
    def __init__(self, model_dir: Path | None = None) -> None:
        if model_dir is not None:
            # Caller (lifespan) already resolved the directory — skip local discovery.
            model_dir = Path(model_dir)
            resolved_dir, deployed_at, source_model_id = (
                model_dir,
                _deployed_at_from_dir(model_dir),
                None,
            )
        else:
            resolved_dir, deployed_at, source_model_id = _resolve_model_dir()
        model_dir = resolved_dir

        model_file = next(model_dir.glob("*.onnx"), None)
        if model_file is None:
            raise FileNotFoundError(f"No .onnx file in {model_dir}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = settings.ort_intra_threads
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        self._session = ort.InferenceSession(
            str(model_file),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
        self._input_names = {inp.name for inp in self._session.get_inputs()}

        config = json.loads((model_dir / "config.json").read_text())
        self.model_id = source_model_id or config.get("_name_or_path") or str(model_dir)
        quant_tag = model_dir.name  # "int8", "o2", or "fp32"
        self.model_version = f"sentinel-roberta-{deployed_at}-{quant_tag}"
        self.threshold = settings.classify_threshold
        self.model_path = str(model_dir)

        logger.info(
            "Loaded %s | version=%s | file=%s | intra_threads=%d | threshold=%.2f",
            self.model_id,
            self.model_version,
            model_file.name,
            settings.ort_intra_threads,
            settings.classify_threshold,
        )

    def warmup(self) -> None:
        self.predict(["warmup"])
        logger.info("Warmup complete")

    def predict(self, texts: list[str]) -> list[dict]:
        inputs = self._tokenizer(
            texts,
            return_tensors="np",
            # Reverted from padding="max_length": every batch then tokenized
            # to the full 512 tokens regardless of actual input length,
            # which OOM-killed the pod (1Gi limit) twice under real load —
            # reproduced live, not theoretical. The "latency doesn't depend
            # on batch content" benefit isn't worth crashing the service;
            # a real fix for that (e.g. bucketed padding, or raising the
            # memory limit + confirming it's stable under sustained load)
            # needs its own verification pass, not a same-session swap back.
            padding=True,
            truncation=True,
            max_length=512,
        )
        ort_inputs = {k: v for k, v in inputs.items() if k in self._input_names}
        logits = self._session.run(["logits"], ort_inputs)[0]

        if logits.shape[-1] == 1:
            # Single-logit binary head: sigmoid gives P(harm) directly.
            scores = _sigmoid(logits).squeeze(axis=-1)
        else:
            # Multi-class softmax head: take P(last class) as the harm score.
            # Assumes label ordering: 0 = safe, last = harm (standard HF convention).
            exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
            scores = (exp / exp.sum(axis=-1, keepdims=True))[:, -1]

        labels = np.where(scores >= settings.classify_threshold, "harm", "safe")

        return [
            {"label": str(label), "score": round(float(score), 4)}
            for label, score in zip(labels, scores)
        ]
