# ── Stage 1: install runtime dependencies ────────────────────────────────────
FROM python:3.12-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.34" \
        "pymongo>=4.9" \
        "httpx>=0.28" \
        "pydantic-settings>=2.3"

# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

COPY --from=deps /app/.venv /app/.venv

# Source files + the static page — no pyproject.toml, no explanation.md.
COPY *.py ./
COPY static ./static

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
