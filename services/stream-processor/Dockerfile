# ── Stage 1: install runtime dependencies ────────────────────────────────────
FROM python:3.12-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# This list must stay in sync with pyproject.toml dependencies.
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
        "kafka-python-ng>=2.2" \
        "httpx>=0.27" \
        "pymongo>=4.0" \
        "psycopg[binary]>=3.2"

# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=deps /app/.venv /app/.venv

COPY *.py ./

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "main.py"]
