# The official image doesn't bundle a Postgres driver or S3 client — the base
# install only supports SQLite/MySQL backends and local-disk artifact storage.
# psycopg2-binary (backend-store-uri postgresql://...) and boto3
# (default-artifact-root s3://... against MinIO) are both required for the
# `mlflow server` command in mlflow.tf to start at all.
FROM ghcr.io/mlflow/mlflow:v3.13.0

RUN pip install --no-cache-dir psycopg2-binary boto3
