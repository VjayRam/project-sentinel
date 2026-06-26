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
| 1 | Classifier service — FastAPI + ONNX inference + Prometheus metrics | In progress |
| 2 | Model optimization pipeline — ONNX export, O2 graph opt, INT8 quantization | Complete |
| 3 | Local infra — k3d, PostgreSQL, MongoDB, MinIO via Terraform + Helm | Pending |
| 4 | Observability — OTel Collector, Jaeger, Prometheus, Grafana | Pending |
| 5 | Trace ingestion — Kafka consumer, PostgreSQL writes | Pending |
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

Each run gets a UUID and writes artifacts and logs to separate directories:

```
models/<run-id>/fp32/    — FP32 ONNX checkpoint
models/<run-id>/o2/      — O2 optimized checkpoint
models/<run-id>/int8/    — INT8 quantized checkpoint
logs/optimizer/<run-id>/report.json
```

**Run the pipeline:**

```bash
uv run python -m pipelines.optimizer.pipeline \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir models/
```

See `pipelines/optimizer/explanation.md` for a detailed walkthrough of every design decision.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/VjayRam/sentinel
cd sentinel
uv sync --all-packages
```

## Key design decisions

- **Sync route for ONNX inference** — `ORTSession.run()` is a blocking C call; putting it in `async def` blocks the entire event loop
- **Rolling restart for model upgrades** — no `/reload` endpoint; with multiple replicas a single-pod reload causes a silent model version split
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
| Infrastructure | Terraform, Helm, k3d |
| CI/CD | GitHub Actions, GHCR |
