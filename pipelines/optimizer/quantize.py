import logging
import shutil
from pathlib import Path

import onnx
from onnxruntime.quantization import QuantType, quantize_dynamic

logger = logging.getLogger(__name__)

_NON_MODEL_EXTENSIONS = {".json", ".txt"}


def quantize(o2_dir: Path, output_dir: Path) -> Path:
    o2_dir = Path(o2_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    model_in = o2_dir / "model_optimized.onnx"
    model_out = output_dir / "model_quantized.onnx"

    logger.info("Applying dynamic INT8 quantization")
    quantize_dynamic(
        model_input=model_in,
        model_output=model_out,
        weight_type=QuantType.QInt8,
        per_channel=False,
        extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
    )

    for f in o2_dir.iterdir():
        if f.suffix in _NON_MODEL_EXTENSIONS:
            shutil.copy2(f, output_dir / f.name)

    logger.info("INT8 checkpoint saved to %s", output_dir)
    return output_dir
