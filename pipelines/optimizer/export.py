import logging
from pathlib import Path

from optimum.exporters.onnx import main_export

logger = logging.getLogger(__name__)


def export(model_id: str, output_dir: Path, opset: int = 17) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Exporting %s to ONNX FP32 (opset %d)", model_id, opset)
    main_export(
        model_name_or_path=model_id,
        output=output_dir,
        task="text-classification",
        opset=opset,
    )

    logger.info("FP32 checkpoint saved to %s", output_dir)
    return output_dir
