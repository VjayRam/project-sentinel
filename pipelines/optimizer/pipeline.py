import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from pipelines.optimizer.export import export
from pipelines.optimizer.optimize import optimize
from pipelines.optimizer.quantize import quantize
from pipelines.optimizer.registry import DSN, register_model
from pipelines.optimizer.upload import upload_report, upload_stage

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

    # Each tuple: (stage_name, fn, output_dir_name)
    # output_dir_name is the subdirectory written by that stage — also used as
    # the MinIO prefix so the bucket mirrors the local artifact tree exactly.
    stages = [
        ("export", lambda: export(model_id, run_artifacts / "fp32", opset=opset), "fp32"),
        ("optimize", lambda: optimize(run_artifacts / "fp32", run_artifacts / "o2"), "o2"),
        ("quantize", lambda: quantize(run_artifacts / "o2", run_artifacts / "int8"), "int8"),
    ]

    minio_ok = True  # flips False on first upload failure; gates registration

    for name, fn, dir_name in stages:
        logger.info("--- Stage: %s | run_id=%s ---", name, run_id)
        t0 = time.perf_counter()
        out = fn()

        # Upload this stage to MinIO immediately after it completes.
        # On failure, upload_stage logs a warning and returns None — we keep
        # going so the local artifacts are always complete regardless.
        minio_prefix = upload_stage(run_id, dir_name, run_artifacts / dir_name)
        if minio_prefix is None:
            minio_ok = False

        report["stages"][name] = {
            "duration_s": round(time.perf_counter() - t0, 2),
            "output": str(out),
            "minio_path": minio_prefix,
        }

    # Determine model_path: MinIO key when available, local path as fallback.
    # Built from the quantize stage's actual upload_stage() return value
    # rather than re-deriving the bucket name — upload_stage already resolved
    # MINIO_BUCKET, so hardcoding "models" here again risked the two silently
    # diverging if MINIO_BUCKET was ever set to something else.
    if minio_ok:
        model_path = f"{report['stages']['quantize']['minio_path']}/model_quantized.onnx"
        suffix = ""
    else:
        # MinIO was unreachable for at least one stage — fall back to the
        # local int8 path so the registry still records this run.
        model_path = str(run_artifacts / "int8")
        suffix = " (local fallback)"
        logger.warning("MinIO unavailable — registering local path as fallback: %s", model_path)

    logger.info("--- Stage: register | run_id=%s ---", run_id)
    t0 = time.perf_counter()
    with psycopg.connect(DSN) as conn:
        register_model(conn, run_id, model_path)
    report["stages"]["register"] = {
        "duration_s": round(time.perf_counter() - t0, 2),
        "output": f"version={run_id} status=staging{suffix}",
    }

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["model_path"] = model_path

    report_path = run_log / "report.json"
    report_path.write_text(json.dumps(report, indent=2))

    # Upload the report to MinIO so it survives pod termination.
    # Best-effort — a failure here does not fail the pipeline.
    upload_report(run_id, report_path)

    logger.info("Pipeline complete | run_id=%s | model_path=%s", run_id, model_path)
    return report_path


# CLI entry point lives in pipelines/optimizer/__main__.py — run via
# `python -m pipelines.optimizer` rather than `python -m pipelines.optimizer.pipeline`.
