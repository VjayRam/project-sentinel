import pytest


@pytest.fixture
def mock_predict():
    """Replaces Classifier.predict in tests — returns real output shape without loading weights."""

    def _predict(texts: list[str]) -> list[dict]:
        return [{"label": "safe", "score": 0.12}] * len(texts)

    return _predict
