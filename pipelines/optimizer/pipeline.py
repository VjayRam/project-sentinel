import argparse
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipelines.optimizer.export import export
from pipelines.optimizer.optimize import optimize
from pipelines.optimizer.quantize import quantize
from pipelines.optimizer.registry import register_model
from pipelines.optimizer.upload import upload_model

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def run(model_id: str, output_dir: str, log_dir: str = "logs", opset: int = 17) -> Path:
    run_id = str(uuid.uuid4())

    run_artifacts = Path(output_dir) / run_id
    run_artifacts.mkdir(parents=True, exist_ok=True)

    run_log = Path(log_dir) / "optimizer" / run_id
    run_log.mkdir(parents=True, exist_ok=True)

    logger.info("Starting optimizer pipeline | run_id=%s", run_id)

    report: dict = {
        "run_id": run_id,
        "model_id": model_id,
        "opset": opset,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
    }

    stages = [
        ("export", lambda: export(model_id, run_artifacts / "fp32", opset=opset)),
        ("optimize", lambda: optimize(run_artifacts / "fp32", run_artifacts / "o2")),
        ("quantize", lambda: quantize(run_artifacts / "o2", run_artifacts / "int8")),
    ]

    for name, fn in stages:
        logger.info("--- Stage: %s | run_id=%s ---", name, run_id)
        t0 = time.perf_counter()
        out = fn()
        report["stages"][name] = {
            "duration_s": round(time.perf_counter() - t0, 2),
            "output": str(out),
        }

    # Upload the final int8 artifact to MinIO.
    # Separated from the stages loop because register needs the model_path
    # that upload returns — the two steps are sequentially dependent.
    logger.info("--- Stage: upload | run_id=%s ---", run_id)
    t0 = time.perf_counter()
    model_path = upload_model(run_id, run_artifacts / "int8")
    report["stages"]["upload"] = {
        "duration_s": round(time.perf_counter() - t0, 2),
        "output": model_path,
    }

    # Register in model_registry as 'staging'. Promotion to 'active' happens
    # via Airflow after evaluation passes — not here.
    logger.info("--- Stage: register | run_id=%s ---", run_id)
    t0 = time.perf_counter()
    register_model(run_id, model_path)
    report["stages"]["register"] = {
        "duration_s": round(time.perf_counter() - t0, 2),
        "output": f"version={run_id} status=staging",
    }

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["model_path"] = model_path

    report_path = run_log / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Pipeline complete | run_id=%s | model_path=%s", run_id, model_path)
    return report_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ONNX optimization pipeline")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    run(args.model_id, args.output_dir, args.log_dir, args.opset)
