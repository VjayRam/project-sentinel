import logging
from pathlib import Path

from optimum.onnxruntime import ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig

logger = logging.getLogger(__name__)


def quantize(o2_dir: Path, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Applying dynamic INT8 quantization")
    quantizer = ORTQuantizer.from_pretrained(o2_dir)
    quantizer.quantize(
        save_dir=output_dir,
        quantization_config=AutoQuantizationConfig.avx2(
            is_static=False,
            per_channel=False,
        ),
    )

    logger.info("INT8 checkpoint saved to %s", output_dir)
    return output_dir
