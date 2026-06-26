import json
import logging
import os
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


def _resolve_model_dir() -> Path:
    if model_path := os.environ.get("MODEL_PATH"):
        return Path(model_path)

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
    return model_dir


class Classifier:
    def __init__(self) -> None:
        model_dir = _resolve_model_dir()

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
        self.model_id = config.get("_name_or_path", str(model_dir))

        logger.info(
            "Loaded %s | file=%s | intra_threads=%d | threshold=%.2f",
            self.model_id,
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
