# ── Stage 1: install runtime dependencies ────────────────────────────────────
FROM python:3.12-slim AS deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install runtime deps into an isolated venv.
# torch and optimum are excluded — they are optimizer pipeline deps, not serving deps.
# This list matches pyproject.toml after gap #2 is resolved.
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.34" \
        "transformers>=4.48" \
        "onnxruntime>=1.27.0" \
        "prometheus-client>=0.21" \
        "pydantic>=2.10"

# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy the pre-built venv from the deps stage — no build tools needed at runtime.
COPY --from=deps /app/.venv /app/.venv

# Copy source files only (no pyproject.toml, no explanation.md, no test files).
COPY *.py ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
