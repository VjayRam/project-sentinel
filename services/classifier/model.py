import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

_THRESHOLD = float(os.environ.get("CLASSIFY_THRESHOLD", "0.5"))
_INTRA_THREADS = int(os.environ.get("ORT_INTRA_THREADS", "4"))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "uv.lock").exists():
            return candidate
    return here


def _resolve_model_dir() -> tuple[Path, str, str | None]:
    """Returns (model_dir, deployed_at, source_model_id).

    source_model_id is the HuggingFace model ID from the optimizer report, or
    None when the path is supplied explicitly via MODEL_PATH.
    """
    if model_path := os.environ.get("MODEL_PATH"):
        p = Path(model_path)
        onnx_file = next(p.glob("*.onnx"), None)
        mtime = onnx_file.stat().st_mtime if onnx_file else None
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else datetime.now(timezone.utc)
        return p, ts.strftime("%Y%m%dT%H%M%SZ"), None

    log_root = _project_root() / "logs" / "optimizer"
    if not log_root.exists():
        raise RuntimeError("No MODEL_PATH set and logs/optimizer/ not found")

    reports = sorted(
        log_root.glob("*/report.json"),
        key=lambda p: json.loads(p.read_text()).get("completed_at", ""),
        reverse=True,
    )
    if not reports:
        raise RuntimeError("No completed optimization runs found in logs/optimizer/")

    report = json.loads(reports[0].read_text())
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
            onnx_file = next(model_dir.glob("*.onnx"), None)
            mtime = onnx_file.stat().st_mtime if onnx_file else None
            ts = datetime.fromtimestamp(mtime, tz=timezone.utc) if mtime else datetime.now(timezone.utc)
            resolved_dir, deployed_at, source_model_id = model_dir, ts.strftime("%Y%m%dT%H%M%SZ"), None
        else:
            resolved_dir, deployed_at, source_model_id = _resolve_model_dir()
        model_dir = resolved_dir

        model_file = next(model_dir.glob("*.onnx"), None)
        if model_file is None:
            raise FileNotFoundError(f"No .onnx file in {model_dir}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = _INTRA_THREADS
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
        model_name = self.model_id.split("/")[-1]
        self.model_version = f"{model_name}-{deployed_at}"
        self.threshold = _THRESHOLD
        self.model_path = str(model_dir)

        logger.info(
            "Loaded %s | version=%s | file=%s | intra_threads=%d | threshold=%.2f",
            self.model_id,
            self.model_version,
            model_file.name,
            _INTRA_THREADS,
            _THRESHOLD,
        )

    def warmup(self) -> None:
        self.predict(["warmup"])
        logger.info("Warmup complete")

    def predict(self, texts: list[str]) -> list[dict]:
        inputs = self._tokenizer(
            texts,
            return_tensors="np",
            padding=True,
            truncation=True,
            max_length=512,
        )
        ort_inputs = {k: v for k, v in inputs.items() if k in self._input_names}
        logits = self._session.run(["logits"], ort_inputs)[0]

        scores = _sigmoid(logits).squeeze(axis=-1)
        labels = np.where(scores >= _THRESHOLD, "harm", "safe")

        return [
            {"label": str(label), "score": round(float(score), 4)}
            for label, score in zip(labels, scores)
        ]
