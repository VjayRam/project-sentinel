import logging
from pathlib import Path

from optimum.onnxruntime import ORTOptimizer
from optimum.onnxruntime.configuration import OptimizationConfig

logger = logging.getLogger(__name__)


def optimize(fp32_dir: Path, output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Applying O2 graph optimization")
    optimizer = ORTOptimizer.from_pretrained(fp32_dir)
    optimizer.optimize(
        save_dir=output_dir,
        optimization_config=OptimizationConfig(
            optimization_level=2,
            optimize_for_gpu=False,
            fp16=False,
        ),
    )

    logger.info("O2 checkpoint saved to %s", output_dir)
    return output_dir
