import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_BUCKET = os.getenv("MINIO_BUCKET", "models")


@lru_cache(maxsize=1)
def _s3_client():
    # Cached — a fresh boto3 client means a new TLS handshake + credential
    # resolution on every call otherwise. Not literally shared with
    # services/classifier/download.py's near-identical factory — separately
    # deployed packages, see that file's comment for why.
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "sentinel"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "sentinel-minio"),
        config=Config(
            signature_version="s3v4",
            # Short timeout so a missing MinIO fails fast instead of hanging
            # the pipeline for the default 60 seconds.
            connect_timeout=5,
            retries={"max_attempts": 2},
        ),
        region_name="us-east-1",
    )


def upload_report(run_id: str, report_path: Path) -> str | None:
    """Upload the pipeline report JSON to MinIO at models/<run-id>/report.json.

    Called after the report is written locally so it survives pod termination.
    Returns the MinIO key on success, None if MinIO unavailable (non-fatal).
    """
    try:
        s3 = _s3_client()
        key = f"{run_id}/report.json"
        logger.info("Uploading report → s3://%s/%s", _BUCKET, key)
        s3.upload_file(str(report_path), _BUCKET, key)
        logger.info("Report uploaded | key=%s/%s", _BUCKET, key)
        return f"{_BUCKET}/{key}"
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.warning("MinIO report upload failed — report kept locally only. Reason: %s", exc)
        return None


def upload_benchmark_report(run_id: str, report_path: Path) -> str | None:
    """Upload a benchmark report JSON to MinIO at models/<run-id>/benchmark_report.json.

    A separate key from upload_report's report.json (the optimizer's own
    stage-timing report) — this one lets a *later* retrain's quality gate
    look up a specific model_version's accuracy/f1/etc. as a regression
    baseline (see pipelines/retraining/pipeline.py's _get_baseline_report)
    without re-running inference against it. Same non-fatal-on-failure
    contract as upload_report.
    """
    try:
        s3 = _s3_client()
        key = f"{run_id}/benchmark_report.json"
        logger.info("Uploading benchmark report → s3://%s/%s", _BUCKET, key)
        s3.upload_file(str(report_path), _BUCKET, key)
        return f"{_BUCKET}/{key}"
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.warning(
            "MinIO benchmark report upload failed — report kept locally only. Reason: %s", exc
        )
        return None


def download_report(run_id: str, filename: str) -> dict | None:
    """Download and parse a JSON report from MinIO at models/<run-id>/<filename>.

    Returns None whenever the report isn't available for any reason —
    MinIO unreachable, the key doesn't exist (e.g. a run that predates
    benchmark-report uploads), or the body isn't valid JSON. Callers should
    treat None as "no data to use," not as an error to propagate; the two
    failure modes aren't distinguished on purpose, since every caller's
    correct response to either is the same (fall back to not having this
    report).
    """
    try:
        s3 = _s3_client()
        key = f"{run_id}/{filename}"
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except (BotoCoreError, ClientError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not download/parse s3://%s/%s: %s", _BUCKET, f"{run_id}/{filename}", exc
        )
        return None


def upload_stage(run_id: str, stage: str, stage_dir: Path) -> str | None:
    """Upload all files in stage_dir to MinIO under models/<run-id>/<stage>/.

    Called after each pipeline stage so MinIO mirrors the full artifact tree:
        models/<run-id>/fp32/   — exported ONNX
        models/<run-id>/o2/     — O2 optimised
        models/<run-id>/int8/   — INT8 quantised (what the classifier loads)

    Returns the MinIO prefix string (models/<run-id>/<stage>) on success,
    or None when MinIO is unreachable — the caller falls back to local storage.
    """
    stage_dir = Path(stage_dir)
    files = [f for f in sorted(stage_dir.iterdir()) if f.is_file()]
    if not files:
        logger.warning("No files found in %s — skipping upload", stage_dir)
        return None

    try:
        s3 = _s3_client()

        def _upload_one(file: Path) -> None:
            key = f"{run_id}/{stage}/{file.name}"
            logger.info("Uploading %s → s3://%s/%s", file.name, _BUCKET, key)
            s3.upload_file(str(file), _BUCKET, key)

        # boto3 clients are thread-safe for concurrent calls. Parallelizing
        # matters most for the fp32 stage (full model + tokenizer files);
        # list(...) forces the map to completion and re-raises the first
        # exception, so a failed upload still hits the except block below
        # exactly like the old sequential loop did.
        with ThreadPoolExecutor(max_workers=min(8, len(files))) as pool:
            list(pool.map(_upload_one, files))

        prefix = f"{_BUCKET}/{run_id}/{stage}"
        logger.info("Stage '%s' uploaded | files=%d | prefix=%s", stage, len(files), prefix)
        return prefix

    except (BotoCoreError, ClientError, OSError) as exc:
        logger.warning(
            "MinIO unavailable for stage '%s' — falling back to local storage. Reason: %s",
            stage,
            exc,
        )
        return None
