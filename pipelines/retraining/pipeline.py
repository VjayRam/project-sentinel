"""Orchestrates the retraining pipeline: build the fine-tuning dataset from
manually-labelled flagged_content (+ optional initial sample), fine-tune,
then hand off to the existing optimizer (ONNX export/quantize/register-as-
staging) and evaluation (quality gate) pipelines unchanged — same shape as
pipelines/optimizer/pipeline.py's run().
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import mlflow
import pymongo

from pipelines.evaluation.benchmark import run as run_benchmark
from pipelines.evaluation.validate import validate
from pipelines.optimizer.pipeline import run as run_optimizer
from pipelines.retraining.dataset import build_dataset
from pipelines.retraining.train import train

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def run(
    mongo_uri: str,
    base_model_id: str,
    output_dir: str,
    log_dir: str = "logs",
    initial_dataset_path: str | None = None,
    sample_size: int = 500,
    epochs: int = 3,
) -> Path:
    run_id = str(uuid.uuid4())
    run_artifacts = Path(output_dir) / run_id
    run_artifacts.mkdir(parents=True, exist_ok=True)
    run_log = Path(log_dir) / "retraining" / run_id
    run_log.mkdir(parents=True, exist_ok=True)

    logger.info("Starting retraining pipeline | run_id=%s", run_id)

    mongo_db = pymongo.MongoClient(mongo_uri).get_default_database()
    dataset = build_dataset(mongo_db, initial_dataset_path, sample_size)

    # MLFLOW_TRACKING_URI is env-var driven (mlflow's client reads it
    # automatically) rather than set here, matching how DATABASE_URL/
    # MONGO_URI are threaded through as plain args/env everywhere else in
    # this repo rather than hardcoded.
    mlflow.set_experiment("sentinel-retraining")
    with mlflow.start_run(run_name=run_id) as mlflow_run:
        mlflow_run_id = mlflow_run.info.run_id
        train_result = train(dataset, base_model_id, run_artifacts / "finetuned", epochs=epochs)
        checkpoint_dir = train_result.pop("checkpoint_dir")

    # Reuses pipelines/optimizer unchanged — the fine-tuned checkpoint plugs
    # in as model_id since export() passes it straight to optimum's
    # main_export(), which accepts a local directory just as readily as a
    # HuggingFace hub id. Registers as 'staging' — promotion to 'active' is
    # retrain_dag.py's job after the quality gate below passes.
    optimizer_report_path = run_optimizer(
        model_id=str(checkpoint_dir), output_dir=output_dir, log_dir=log_dir
    )
    optimizer_report = json.loads(optimizer_report_path.read_text())
    model_path = optimizer_report["model_path"]
    int8_dir = Path(optimizer_report["stages"]["quantize"]["output"])

    benchmark_report = run_benchmark(model_dir=str(int8_dir))
    gate_passed, reasons = validate(benchmark_report)

    report = {
        "run_id": run_id,
        "mlflow_run_id": mlflow_run_id,
        "base_model_id": base_model_id,
        "dataset_sources": dataset["sources"],
        "train_size": len(dataset["train"]),
        "val_size": len(dataset["val"]),
        "final_train_eval_metrics": train_result,
        "benchmark": benchmark_report,
        "gate_passed": gate_passed,
        "gate_reasons": reasons,
        "model_version": optimizer_report["run_id"],
        "model_path": model_path,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    report_path = run_log / "report.json"
    report_path.write_text(json.dumps(report, indent=2))

    # KubernetesPodOperator's XCom sidecar tails this exact path — a plain
    # existence check, no Airflow import needed, so this pipeline stays a
    # normal standalone script runnable outside Airflow too.
    xcom_dir = Path("/airflow/xcom")
    if xcom_dir.exists():
        (xcom_dir / "return.json").write_text(json.dumps(report))

    logger.info(
        "Retraining pipeline complete | run_id=%s | gate_passed=%s | model_version=%s",
        run_id,
        gate_passed,
        report["model_version"],
    )
    return report_path


# CLI entry point lives in pipelines/retraining/__main__.py — run via
# `python -m pipelines.retraining` rather than `python -m pipelines.retraining.pipeline`.
