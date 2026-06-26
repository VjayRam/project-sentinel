import pytest


@pytest.fixture
def mock_classifier():
    """Stub for the ONNX/transformers classifier.

    Tests import this fixture instead of loading the real model,
    so CI never needs torch or model weights.
    """

    def _classify(text: str):
        return [{"label": "LABEL_0", "score": 0.95}]

    return _classify
