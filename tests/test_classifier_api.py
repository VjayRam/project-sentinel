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


# ── /health/live + /health/ready ─────────────────────────────────────────────


def test_liveness_always_ok(client):
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_ok_when_ready(client):
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── /v1/moderations — single string input (goes through DynamicBatcher) ───────


def test_moderations_single_response_shape(client):
    r = client.post("/v1/moderations", json={"input": "hello world"})
    assert r.status_code == 200
    assert set(r.json().keys()) == {"id", "model", "results"}
    assert len(r.json()["results"]) == 1


def test_moderations_single_result_shape(client):
    r = client.post("/v1/moderations", json={"input": "hello world"})
    result = r.json()["results"][0]
    assert set(result.keys()) == {"flagged", "categories", "category_scores"}
    assert isinstance(result["flagged"], bool)
    assert 0.0 <= result["category_scores"]["harm"] <= 1.0


def test_moderations_missing_input_returns_422(client):
    r = client.post("/v1/moderations", json={})
    assert r.status_code == 422


# ── /v1/moderations — list input (dispatched directly, no batcher queue) ──────


def test_moderations_batch_result_count_matches_input(client):
    texts = ["hello", "world", "foo"]
    r = client.post("/v1/moderations", json={"input": texts})
    assert r.status_code == 200
    assert len(r.json()["results"]) == len(texts)


def test_moderations_batch_each_result_shape(client):
    r = client.post("/v1/moderations", json={"input": ["a", "b"]})
    for result in r.json()["results"]:
        assert isinstance(result["flagged"], bool)
        assert 0.0 <= result["category_scores"]["harm"] <= 1.0


def test_moderations_batch_empty_list_returns_422(client):
    r = client.post("/v1/moderations", json={"input": []})
    assert r.status_code == 422


def test_moderations_batch_over_max_size_returns_422(client):
    r = client.post("/v1/moderations", json={"input": ["x"] * 65})
    assert r.status_code == 422


def test_moderations_batch_exactly_max_size_accepted(client):
    r = client.post("/v1/moderations", json={"input": ["x"] * 64})
    assert r.status_code == 200


def test_moderations_skip_persist_header_accepted(client):
    r = client.post(
        "/v1/moderations",
        json={"input": "hello"},
        headers={"X-Sentinel-Skip-Persist": "true"},
    )
    assert r.status_code == 200


# ── /metrics ──────────────────────────────────────────────────────────────────


def test_metrics_endpoint_responds(client):
    r = client.get("/metrics/")
    assert r.status_code == 200
    assert "classifier_requests_total" in r.text
