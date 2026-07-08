# syntax=docker/dockerfile:1
# Build context MUST be the repo root, not this directory — unlike drift/'s
# self-contained Dockerfile, pipeline.py imports pipelines.optimizer,
# pipelines.evaluation, and datasets.eval_holdout (transitively, via
# benchmark.py) directly, so all three trees need to be in the build context:
#   docker build -f pipelines/retraining/Dockerfile -t sentinel-retraining:local .
FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# CPU-only torch, explicitly — this pod runs inside k3d, which has no GPU
# passthrough configured (adding it was out of scope for this phase; see
# pipelines/retraining/explanation.md). Installing the default (CUDA) wheel
# here would pull in ~2GB of CUDA runtime this pod can never use.
#
# torch's wheel alone is ~183MB; on a slow/flaky link uv's default 30s idle
# timeout trips mid-download and the whole layer fails with "network
# timeout" (live-verified — see git history). UV_HTTP_TIMEOUT raises that
# budget, and the cache mount means a retried build resumes from
# already-downloaded wheels instead of starting over from zero.
ENV UV_HTTP_TIMEOUT=300
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2" && \
    uv pip install --python /app/.venv/bin/python \
        "transformers>=4.48" \
        "accelerate>=0.26.0" \
        "mlflow>=2.19" \
        "pymongo>=4.9" \
        "numpy>=2.0" \
        "psycopg[binary]>=3.2" \
        "optimum[onnxruntime]>=2.1.0" \
        "onnxruntime>=1.27.0" \
        "onnx>=1.17" \
        "boto3>=1.35"

ENV PATH="/app/.venv/bin:$PATH"

# pipelines/retraining imports pipelines.optimizer and pipelines.evaluation
# directly (pipeline.py) — copy the whole pipelines/ and datasets/ tree, not
# just this package, so those imports resolve without a workspace install.
COPY pipelines /app/pipelines
COPY datasets /app/datasets

WORKDIR /app

CMD ["python", "-m", "pipelines.retraining"]
