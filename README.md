# Sentinel

A content safety monitoring platform for LLM applications. Sentinel receives OpenTelemetry traces from a chat app, classifies prompt/response text for toxicity, detects when the model's input distribution drifts, and automatically retrains and redeploys a new model version.

Built as a learning project — every tool is introduced one phase at a time with production-grade patterns.

## What it does

```
Chat app
  → OTel spans (OTLP/gRPC)
  → OTel Collector
  → Kafka topic: traces.raw
  → Stream processor
  → POST /classify  (classifier service)
  → PostgreSQL: classifications table
  → MongoDB: flagged_content (harmful only, for retraining)
  → Spark: drift metrics → drift_stats table
  → Airflow DAG: triggers retrain when PSI > 0.2
  → Rolling restart: pods load new model from model registry
```

## Project structure

```
sentinel/
  services/
    classifier/        — FastAPI inference service (ONNX Runtime)
  pipelines/
    optimizer/         — ONNX export + graph optimization + INT8 quantization
    evaluation/        — Latency benchmarks and accuracy regression checks
  tests/               — Workspace-level tests
  infra/               — Terraform + Helm charts (Phase 3+)
  dags/                — Airflow DAG definitions (Phase 7)
```

## Build phases

| Phase | What gets built | Status |
|-------|----------------|--------|
| 1 | Classifier service — FastAPI + ONNX inference + Prometheus metrics | Complete ✓ |
| 2 | Model optimization pipeline — ONNX export, O2 graph opt, INT8 quantization | Complete ✓ |
| 3 | Local infra — k3d, PostgreSQL, MongoDB, MinIO via Terraform | Complete ✓ |
| 4 | Observability — Prometheus, Grafana | Complete ✓ |
| 5 | Trace ingestion — OTel Collector, Kafka consumer, PostgreSQL + MongoDB writes | Pending |
| 6 | Drift detection — PySpark, PSI/JSD metrics | Pending |
| 7 | Orchestration — Airflow DAGs, MLflow model registry | Pending |
| 8 | Cloud deployment — EKS/GKE, RDS, S3 via Terraform workspaces | Pending |

## Model optimization pipeline (Phase 2)

Converts a fine-tuned RoBERTa classifier from HuggingFace into a production-ready ONNX INT8 model.

**Three-stage pipeline:**

| Stage | Input | Output | Size | Notes |
|-------|-------|--------|------|-------|
| Export | HF Hub model | `model.onnx` | 476 MB | FP32, opset 18 |
| Optimize | `model.onnx` | `model_optimized.onnx` | 476 MB | O2 graph fusions, zero accuracy loss |
| Quantize | `model_optimized.onnx` | `model_quantized.onnx` | 120 MB | Dynamic INT8, <0.2% accuracy loss |

Each run gets a UUID. Artifacts are written locally and uploaded to MinIO:

```
artifacts/<run-id>/fp32/    — FP32 ONNX + tokenizer (local only, gitignored)
artifacts/<run-id>/o2/      — O2 optimized checkpoint (local only, gitignored)
artifacts/<run-id>/int8/    — INT8 quantized checkpoint (local only, gitignored)
logs/optimizer/<run-id>/report.json

MinIO models/<run-id>/fp32/       — FP32 artifacts
MinIO models/<run-id>/o2/         — O2 artifacts
MinIO models/<run-id>/int8/       — INT8 artifacts (what the classifier loads)
MinIO models/<run-id>/report.json — full pipeline report
```

**Run the pipeline:**

```bash
uv run --package sentinel-optimizer python -m pipelines.optimizer.pipeline \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir artifacts
```

See `pipelines/optimizer/explanation.md` for a detailed walkthrough of every design decision.

## Model lifecycle

The optimizer, registry, and classifier are wired together through PostgreSQL and MinIO:

```
optimizer run
  → uploads fp32/o2/int8 artifacts to MinIO
  → writes row to model_registry (status = staging)

classifier pod startup
  → queries model_registry for active model (falls back to most-recent staging)
  → downloads int8 artifacts from MinIO to /tmp/sentinel-model-cache/<run-id>/int8/
  → loads ONNX model and tokenizer
  → registers itself in model_registry (idempotent)

Airflow retrain_dag (Phase 7)
  → promotes staging → active in model_registry
  → runs kubectl rollout restart deployment/classifier
  → new pods pick up the promoted model on next startup
```

Model upgrades always go through rolling restart — no `/reload` endpoint. With multiple replicas, an in-process reload would hit only one pod, causing a silent model version split.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/VjayRam/sentinel
cd sentinel
uv sync --all-packages
```

For local dev with the full stack (k3d cluster + databases + monitoring):

```bash
./scripts/dev-start.sh
```

See `docs/local-dev.md` for full reference.

## Key design decisions

- **Sync route for ONNX inference** — `ORTSession.run()` is a blocking C call; putting it in `async def` blocks the entire event loop
- **Rolling restart for model upgrades** — no `/reload` endpoint; with multiple replicas a single-pod reload causes a silent model version split
- **model_registry as source of truth** — classifier queries PostgreSQL on startup to find the active model; `MODEL_PATH` env var is only the fallback when DB is unreachable
- **Manual Kafka offset commit** — committed only after a successful PostgreSQL write; `ON CONFLICT DO NOTHING` handles reprocessing on retry
- **PSI > 0.2 triggers retrain** — Population Stability Index and Jensen-Shannon Divergence as drift metrics
- **Dynamic INT8 quantization** — weights quantized offline, activations at runtime; no calibration dataset needed; 75% size reduction, ~3x latency improvement, <0.2% accuracy cost

## Tech stack

| Concern | Tool |
|---------|------|
| Inference API | FastAPI, ONNX Runtime |
| Model optimization | HuggingFace Optimum, ONNX Runtime |
| Stream processing | Kafka (Strimzi), Python consumer |
| Databases | PostgreSQL (classifications), MongoDB (flagged content) |
| Object storage | MinIO |
| Drift detection | PySpark, PSI/JSD |
| Orchestration | Apache Airflow |
| Experiment tracking | MLflow |
| Observability | OTel Collector, Prometheus, Grafana, Jaeger |
| Infrastructure | Terraform, k3d |
| CI/CD | GitHub Actions, GHCR |
