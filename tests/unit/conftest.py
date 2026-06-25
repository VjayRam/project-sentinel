"""
Patch heavy ML dependencies in sys.modules before any test file imports them.
This lets us import services/classifier/main.py and ml/optimization/optimize.py
without installing onnxruntime, torch, or optimum.

pytest loads conftest.py before collecting tests, so these patches are in place
before any `from services.classifier.main import app` runs.
"""

import os
import sys
from unittest.mock import MagicMock

import numpy as np

# ── environment — prevent DB connections and real model file lookups ──────────
os.environ.pop("SENTINEL_DB_URL", None)
os.environ["MODEL_PATH"] = "/fake/model.onnx"
os.environ["MODEL_VERSION"] = "test-v1"
os.environ["TOKENIZER_PATH"] = "/fake/tokenizer"

# ── onnxruntime ───────────────────────────────────────────────────────────────
_mock_ort = MagicMock(name="onnxruntime")
_mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
_mock_ort.ExecutionMode.ORT_SEQUENTIAL = 0

# Default logits: safe wins (class 0).  Tests override per-call via SESSION.run.return_value.
ort_session = MagicMock(name="ort_session")
ort_session.run.return_value = [np.array([[1.5, 0.5]])]

_mock_ort.InferenceSession.return_value = ort_session
_mock_ort.SessionOptions.return_value = MagicMock()

_mock_ort_quant = MagicMock(name="onnxruntime.quantization")
sys.modules["onnxruntime"] = _mock_ort
sys.modules["onnxruntime.quantization"] = _mock_ort_quant

# ── transformers ──────────────────────────────────────────────────────────────
_mock_tokenizer_instance = MagicMock(name="tokenizer")
_mock_tokenizer_instance.return_value = {
    "input_ids": np.array([[101, 999, 102] + [0] * 509], dtype=np.int64),
    "attention_mask": np.array([[1, 1, 1] + [0] * 509], dtype=np.int64),
}

_mock_transformers = MagicMock(name="transformers")
_mock_transformers.AutoTokenizer.from_pretrained.return_value = _mock_tokenizer_instance
sys.modules["transformers"] = _mock_transformers

# ── psycopg2 ──────────────────────────────────────────────────────────────────
sys.modules["psycopg2"] = MagicMock(name="psycopg2")


# ── torch ─────────────────────────────────────────────────────────────────────
# torch.Tensor must be a real class — scipy's array_api_compat calls
# issubclass(x, torch.Tensor), which raises TypeError if Tensor is a MagicMock.
class _FakeTorchTensor:
    pass


_mock_torch = MagicMock(name="torch")
_mock_torch.Tensor = _FakeTorchTensor
_mock_torch.no_grad.return_value.__enter__ = lambda s: s
_mock_torch.no_grad.return_value.__exit__ = lambda s, *a: False
sys.modules["torch"] = _mock_torch

# ── optimum ───────────────────────────────────────────────────────────────────
sys.modules["optimum"] = MagicMock(name="optimum")
sys.modules["optimum.onnxruntime"] = MagicMock(name="optimum.onnxruntime")
sys.modules["optimum.onnxruntime.configuration"] = MagicMock(
    name="optimum.onnxruntime.configuration"
)
