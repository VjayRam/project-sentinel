"""
Classifier API tests. Classifier is patched so no model weights or torch are
needed — tests verify request/response contracts, not model accuracy.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    mock_clf = MagicMock()
    mock_clf.predict.side_effect = lambda texts: [{"label": "safe", "score": 0.12}] * len(texts)
    mock_clf.model_id = "VijayRam1812/content-classifier-roberta"
    mock_clf.model_version = "content-classifier-roberta-20260101T000000Z"

    # Patch main.Classifier — main.py binds the name at import time via
    # `from model import Classifier`, so we patch the name in main's namespace.
    with patch("main.Classifier", return_value=mock_clf):
        from main import app

        with TestClient(app) as c:
            yield c


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── /classify ─────────────────────────────────────────────────────────────────


def test_classify_response_shape(client):
    r = client.post("/classify", json={"text": "hello world"})
    assert r.status_code == 200
    assert set(r.json().keys()) == {"label", "score", "latency_ms", "model_version", "inference_at"}


def test_classify_label_is_valid(client):
    r = client.post("/classify", json={"text": "hello world"})
    assert r.json()["label"] in ("safe", "harm")


def test_classify_score_in_range(client):
    r = client.post("/classify", json={"text": "hello world"})
    assert 0.0 <= r.json()["score"] <= 1.0


def test_classify_latency_non_negative(client):
    r = client.post("/classify", json={"text": "hello world"})
    assert r.json()["latency_ms"] >= 0


def test_classify_missing_text_returns_422(client):
    r = client.post("/classify", json={})
    assert r.status_code == 422


# ── /classify/batch ───────────────────────────────────────────────────────────


def test_batch_response_shape(client):
    r = client.post("/classify/batch", json={"texts": ["hello", "world"]})
    assert r.status_code == 200
    assert set(r.json().keys()) == {
        "results",
        "latency_ms",
        "batch_size",
        "model_version",
        "inference_at",
    }


def test_batch_result_count_matches_input(client):
    texts = ["hello", "world", "foo"]
    r = client.post("/classify/batch", json={"texts": texts})
    body = r.json()
    assert body["batch_size"] == len(texts)
    assert len(body["results"]) == len(texts)


def test_batch_each_result_shape(client):
    r = client.post("/classify/batch", json={"texts": ["a", "b"]})
    for result in r.json()["results"]:
        assert result["label"] in ("safe", "harm")
        assert 0.0 <= result["score"] <= 1.0


def test_batch_empty_list_returns_422(client):
    r = client.post("/classify/batch", json={"texts": []})
    assert r.status_code == 422


def test_batch_over_max_size_returns_422(client):
    r = client.post("/classify/batch", json={"texts": ["x"] * 65})
    assert r.status_code == 422


def test_batch_exactly_max_size_accepted(client):
    r = client.post("/classify/batch", json={"texts": ["x"] * 64})
    assert r.status_code == 200


def test_batch_missing_field_returns_422(client):
    r = client.post("/classify/batch", json={})
    assert r.status_code == 422


# ── /metrics ──────────────────────────────────────────────────────────────────


def test_metrics_endpoint_responds(client):
    r = client.get("/metrics/")
    assert r.status_code == 200
    assert "classifier_requests_total" in r.text
