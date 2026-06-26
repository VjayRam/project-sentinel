import logging
from pathlib import Path

from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)


def export(model_id: str, output_dir: Path, opset: int = 17) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Exporting %s to ONNX FP32 (opset %d)", model_id, opset)
    model = ORTModelForSequenceClassification.from_pretrained(
        model_id,
        export=True,
        opset=opset,
    )
    model.save_pretrained(output_dir)

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(output_dir)

    logger.info("FP32 checkpoint saved to %s", output_dir)
    return output_dir
