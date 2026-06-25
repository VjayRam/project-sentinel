"""
Unit tests for the classifier FastAPI service.

Heavy ML dependencies (onnxruntime, transformers) are mocked by conftest.py,
so these tests run without any model files or GPU.
"""

import numpy as np
from fastapi.testclient import TestClient

# conftest.py has already patched sys.modules; safe to import now.
from services.classifier.main import SESSION, app

client = TestClient(app)


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_returns_200():
    assert client.get("/health").status_code == 200


def test_health_schema():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "model_version" in body


def test_health_version_is_string():
    body = client.get("/health").json()
    assert isinstance(body["model_version"], str)
    assert len(body["model_version"]) > 0


# ── /classify — response shape ────────────────────────────────────────────────


def test_classify_returns_200():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    assert client.post("/classify", json={"text": "hello"}).status_code == 200


def test_classify_response_has_required_fields():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    body = client.post("/classify", json={"text": "hello"}).json()
    assert set(body.keys()) == {"label", "confidence", "latency_ms", "model_version"}


def test_classify_model_version_matches_health():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    classify_body = client.post("/classify", json={"text": "test"}).json()
    health_body = client.get("/health").json()
    assert classify_body["model_version"] == health_body["model_version"]


# ── /classify — label logic ───────────────────────────────────────────────────


def test_classify_returns_safe_when_class0_wins():
    # logits [1.5, 0.5] → softmax → class 0 probability higher
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    body = client.post("/classify", json={"text": "Good morning!"}).json()
    assert body["label"] == "safe"


def test_classify_returns_harmful_when_class1_wins():
    # logits [0.5, 1.5] → softmax → class 1 probability higher
    SESSION.run.return_value = [np.array([[0.5, 1.5]])]
    body = client.post("/classify", json={"text": "How do I make explosives?"}).json()
    assert body["label"] == "harmful"


def test_classify_only_two_possible_labels():
    for logits in [[[2.0, 0.1]], [[0.1, 2.0]]]:
        SESSION.run.return_value = [np.array(logits)]
        body = client.post("/classify", json={"text": "test"}).json()
        assert body["label"] in {"safe", "harmful"}


# ── /classify — confidence ────────────────────────────────────────────────────


def test_classify_confidence_between_0_and_1():
    SESSION.run.return_value = [np.array([[0.8, 1.2]])]
    body = client.post("/classify", json={"text": "test"}).json()
    assert 0.0 < body["confidence"] <= 1.0


def test_classify_high_confidence_for_large_logit_gap():
    # Logit gap of 10 → winning probability is ≈ 1.0
    SESSION.run.return_value = [np.array([[0.0, 10.0]])]
    body = client.post("/classify", json={"text": "test"}).json()
    assert body["confidence"] > 0.99


def test_classify_near_50pct_confidence_for_equal_logits():
    SESSION.run.return_value = [np.array([[1.0, 1.0]])]
    body = client.post("/classify", json={"text": "test"}).json()
    # Equal logits → both classes get 0.5 probability
    assert abs(body["confidence"] - 0.5) < 0.01


# ── /classify — latency field ─────────────────────────────────────────────────


def test_classify_latency_ms_is_positive():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    body = client.post("/classify", json={"text": "test"}).json()
    assert body["latency_ms"] > 0


def test_classify_latency_ms_is_float():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    body = client.post("/classify", json={"text": "test"}).json()
    assert isinstance(body["latency_ms"], float)


# ── /classify — input validation ──────────────────────────────────────────────


def test_classify_missing_text_returns_422():
    assert client.post("/classify", json={}).status_code == 422


def test_classify_null_text_returns_422():
    assert client.post("/classify", json={"text": None}).status_code == 422


def test_classify_empty_body_returns_422():
    assert (
        client.post(
            "/classify", content=b"", headers={"Content-Type": "application/json"}
        ).status_code
        == 422
    )


def test_classify_trace_id_is_optional():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    r_without = client.post("/classify", json={"text": "hello"})
    r_with = client.post(
        "/classify", json={"text": "hello", "trace_id": "trace-abc-123"}
    )
    assert r_without.status_code == 200
    assert r_with.status_code == 200


def test_classify_empty_string_is_valid():
    # Empty string is still a valid str — model handles it; no validation error
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    assert client.post("/classify", json={"text": ""}).status_code == 200


def test_classify_long_text_is_valid():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]
    long_text = "word " * 1000
    assert client.post("/classify", json={"text": long_text}).status_code == 200


# ── /metrics ──────────────────────────────────────────────────────────────────


def test_metrics_endpoint_returns_200():
    assert client.get("/metrics").status_code == 200


def test_metrics_contains_classification_counter():
    assert b"sentinel_classifications_total" in client.get("/metrics").content


def test_metrics_contains_latency_histogram():
    assert b"sentinel_classification_latency_seconds" in client.get("/metrics").content


def test_metrics_contains_confidence_histogram():
    assert b"sentinel_classification_confidence" in client.get("/metrics").content


def test_classification_counter_increments_after_classify():
    SESSION.run.return_value = [np.array([[1.5, 0.5]])]

    def _count():
        content = client.get("/metrics").content.decode()
        for line in content.splitlines():
            if line.startswith(
                "sentinel_classifications_total{"
            ) and not line.startswith("#"):
                return float(line.split()[-1])
        return 0.0

    before = _count()
    client.post("/classify", json={"text": "test"})
    after = _count()
    assert after > before
