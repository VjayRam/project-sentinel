import logging
import os
from pathlib import Path

import boto3
from botocore.client import Config

logger = logging.getLogger(__name__)

_BUCKET = os.getenv("MINIO_BUCKET", "models")


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY", "sentinel"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY", "sentinel-minio"),
        # S3v4 is required by MinIO; boto3 defaults to S3v4 but we pin it
        # explicitly so the config is self-documenting.
        config=Config(signature_version="s3v4"),
        # MinIO ignores region_name but boto3 requires a non-empty value.
        region_name="us-east-1",
    )


def upload_model(run_id: str, int8_dir: Path) -> str:
    """Upload all files from int8_dir to MinIO under models/<run_id>/.

    Uploads: model_quantized.onnx + tokenizer/config JSON files copied by
    quantize.py. Skips directories.

    Returns the model_path stored in model_registry:
        models/<run_id>/model_quantized.onnx
    """
    int8_dir = Path(int8_dir)
    s3 = _s3_client()

    uploaded: list[str] = []
    for file in sorted(int8_dir.iterdir()):
        if not file.is_file():
            continue
        key = f"{run_id}/{file.name}"
        logger.info("Uploading %s → s3://%s/%s", file.name, _BUCKET, key)
        s3.upload_file(str(file), _BUCKET, key)
        uploaded.append(key)

    model_path = f"{_BUCKET}/{run_id}/model_quantized.onnx"
    logger.info("Upload complete | files=%d | model_path=%s", len(uploaded), model_path)
    return model_path
