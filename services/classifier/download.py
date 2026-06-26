import logging
import os
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# Local directory where downloaded models are cached between restarts.
# Using /tmp so the cache is pod-local — each pod always downloads fresh
# on the first request for a given version, then hits the cache after that.
_CACHE_ROOT = Path(os.getenv("MODEL_CACHE_DIR", "/tmp/sentinel-model-cache"))


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "sentinel"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "sentinel-minio"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            retries={"max_attempts": 2},
        ),
        region_name="us-east-1",
    )


def _parse_minio_path(model_path: str) -> tuple[str, str]:
    """Parse 'models/<run-id>/int8/model_quantized.onnx' → (bucket, prefix).

    prefix = '<run-id>/int8/' — used for listing all files in that stage dir.
    """
    parts = model_path.split("/")
    bucket = parts[0]
    prefix = "/".join(parts[1:-1]) + "/"
    return bucket, prefix


def download_model(model_path: str) -> Path | None:
    """Resolve a model_registry model_path to a local directory ready for loading.

    Handles two formats stored in model_registry.model_path:

    1. MinIO path  — 'models/<run-id>/int8/model_quantized.onnx'
       Downloads all files from that MinIO prefix to a local cache dir.
       Skips the download if the cache dir already has a .onnx file (cache hit).

    2. Local path  — '/absolute/path/to/int8'
       Returned directly. This is the fallback written by the optimizer when
       MinIO was unreachable at optimization time.

    Returns the local directory Path on success, or None if the model cannot
    be resolved (MinIO unreachable, path not found, etc.).
    """
    # ── local path (MinIO was unavailable when the optimizer ran) ─────────────
    if model_path.startswith("/"):
        local = Path(model_path)
        if local.exists() and any(local.glob("*.onnx")):
            logger.info("Using local model path from registry: %s", local)
            return local
        logger.warning("Local model path in registry not found: %s", local)
        return None

    # ── MinIO path ─────────────────────────────────────────────────────────────
    try:
        bucket, prefix = _parse_minio_path(model_path)
        cache_dir = _CACHE_ROOT / prefix.rstrip("/")

        # Cache hit — skip download if the .onnx is already present.
        if cache_dir.exists() and any(cache_dir.glob("*.onnx")):
            logger.info("Model cache hit | dir=%s", cache_dir)
            return cache_dir

        cache_dir.mkdir(parents=True, exist_ok=True)
        s3 = _s3_client()

        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        objects = response.get("Contents", [])
        if not objects:
            logger.warning("No objects found at s3://%s/%s", bucket, prefix)
            return None

        for obj in objects:
            key = obj["Key"]
            filename = key.split("/")[-1]
            dest = cache_dir / filename
            logger.info("Downloading s3://%s/%s → %s", bucket, key, dest)
            s3.download_file(bucket, key, str(dest))

        logger.info("Download complete | files=%d | dir=%s", len(objects), cache_dir)
        return cache_dir

    except (BotoCoreError, ClientError, OSError) as exc:
        logger.warning("MinIO download failed — will use local model: %s", exc)
        return None
