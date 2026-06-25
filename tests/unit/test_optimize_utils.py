"""
Unit tests for pure utility functions in ml/optimization/optimize.py.

These functions only use numpy and scikit-learn — no model files, no network.
Heavy ML imports (torch, optimum, onnxruntime) are mocked by conftest.py.
"""

import re
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest

from ml.optimization.optimize import (
    _model_version,
    _softmax,
    accuracy_metrics,
    latency_stats,
    register_model,
)


# ── _softmax ──────────────────────────────────────────────────────────────────


def test_softmax_sums_to_one():
    result = _softmax(np.array([1.0, 2.0, 3.0]))
    assert abs(result.sum() - 1.0) < 1e-6


def test_softmax_preserves_order():
    result = _softmax(np.array([0.5, 1.5]))
    assert result[1] > result[0]


def test_softmax_equal_logits_gives_uniform():
    result = _softmax(np.array([1.0, 1.0]))
    assert abs(result[0] - 0.5) < 1e-6
    assert abs(result[1] - 0.5) < 1e-6


def test_softmax_large_gap_approaches_one_hot():
    result = _softmax(np.array([0.0, 100.0]))
    assert result[1] > 0.999


def test_softmax_numerically_stable_with_large_values():
    # Without the max subtraction trick this would overflow
    result = _softmax(np.array([1000.0, 1001.0]))
    assert not np.any(np.isnan(result))
    assert abs(result.sum() - 1.0) < 1e-6


# ── latency_stats ─────────────────────────────────────────────────────────────


def test_latency_stats_returns_three_values():
    p50, p95, p99 = latency_stats([10.0, 20.0, 30.0, 40.0, 50.0])
    assert isinstance(p50, float)
    assert isinstance(p95, float)
    assert isinstance(p99, float)


def test_latency_stats_ordering():
    lats = list(range(1, 101))  # 1..100 ms
    p50, p95, p99 = latency_stats(lats)
    assert p50 <= p95 <= p99


def test_latency_stats_known_values():
    # For a uniform 1..100 range, numpy percentiles are well-defined
    lats = list(range(1, 101))
    p50, p95, p99 = latency_stats(lats)
    assert 49.0 <= p50 <= 51.0
    assert 94.0 <= p95 <= 96.0
    assert 98.0 <= p99 <= 100.0


def test_latency_stats_single_value():
    p50, p95, p99 = latency_stats([42.0])
    assert p50 == 42.0
    assert p95 == 42.0
    assert p99 == 42.0


def test_latency_stats_identical_values():
    p50, p95, p99 = latency_stats([7.5] * 100)
    assert p50 == pytest.approx(7.5)
    assert p95 == pytest.approx(7.5)
    assert p99 == pytest.approx(7.5)


# ── accuracy_metrics ──────────────────────────────────────────────────────────


def test_accuracy_metrics_returns_required_keys():
    labels = [0, 1, 0, 1]
    preds = [0, 1, 0, 1]
    probs = [0.1, 0.9, 0.2, 0.8]
    result = accuracy_metrics(labels, preds, probs)
    assert set(result.keys()) == {"accuracy", "f1", "auc_roc"}


def test_accuracy_metrics_perfect_predictions():
    labels = [0, 1, 0, 1, 0, 1]
    preds = [0, 1, 0, 1, 0, 1]
    probs = [0.1, 0.9, 0.1, 0.9, 0.1, 0.9]
    result = accuracy_metrics(labels, preds, probs)
    assert result["accuracy"] == 1.0
    assert result["f1"] == 1.0
    assert result["auc_roc"] == 1.0


def test_accuracy_metrics_all_wrong():
    labels = [0, 0, 1, 1]
    preds = [1, 1, 0, 0]
    probs = [0.9, 0.8, 0.2, 0.1]  # high prob for class 1, but actual labels are flipped
    result = accuracy_metrics(labels, preds, probs)
    assert result["accuracy"] == 0.0


def test_accuracy_metrics_partial_accuracy():
    labels = [0, 1, 0, 1]
    preds = [0, 0, 0, 1]  # 3 out of 4 correct
    probs = [0.2, 0.4, 0.3, 0.9]
    result = accuracy_metrics(labels, preds, probs)
    assert result["accuracy"] == pytest.approx(0.75)


def test_accuracy_metrics_values_rounded_to_4dp():
    labels = [0, 1, 0, 1, 0]
    preds = [0, 1, 1, 0, 0]
    probs = [0.1, 0.9, 0.6, 0.4, 0.2]
    result = accuracy_metrics(labels, preds, probs)
    for key in ("accuracy", "f1", "auc_roc"):
        # Verify it's rounded to at most 4 decimal places
        assert result[key] == round(result[key], 4)


def test_accuracy_metrics_auc_between_0_and_1():
    labels = [0, 1, 0, 1, 0, 1]
    preds = [0, 1, 0, 0, 0, 1]
    probs = [0.2, 0.8, 0.3, 0.45, 0.1, 0.9]
    result = accuracy_metrics(labels, preds, probs)
    assert 0.0 <= result["auc_roc"] <= 1.0


# ── _model_version ────────────────────────────────────────────────────────────


def test_model_version_format_with_git():
    with patch(
        "ml.optimization.optimize.subprocess.check_output", return_value=b"abc1234\n"
    ):
        version = _model_version()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert version == f"v{today}-abc1234"


def test_model_version_falls_back_when_git_unavailable():
    with patch(
        "ml.optimization.optimize.subprocess.check_output",
        side_effect=FileNotFoundError,
    ):
        version = _model_version()
    assert version.endswith("-local")


def test_model_version_starts_with_v():
    version = _model_version()
    assert version.startswith("v")


def test_model_version_date_portion_is_8_digits():
    version = _model_version()
    # Format: v{YYYYMMDD}-{sha}
    date_part = version[1:].split("-")[0]
    assert re.fullmatch(r"\d{8}", date_part)


def test_model_version_is_unique_across_calls():
    with patch(
        "ml.optimization.optimize.subprocess.check_output", return_value=b"abc1234\n"
    ):
        v1 = _model_version()
        v2 = _model_version()
    assert v1 == v2  # same git SHA + same date = same version (deterministic)


# ── register_model — no-DB paths ──────────────────────────────────────────────


def test_register_model_skips_when_db_url_not_set(capsys):
    import os

    os.environ.pop("SENTINEL_DB_URL", None)
    register_model("v20240101-abc", "/models/model.onnx", {"accuracy": 0.95})
    out = capsys.readouterr().out
    assert "skipping" in out.lower()
