# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

**Sentinel** is a content safety monitoring platform for LLM applications. It receives OpenTelemetry traces from a chat app (an external black box), extracts prompt/response text from span attributes, classifies that content with a fine-tuned toxicity model, detects when the model's input distribution drifts, and automatically retrains + redeploys a new model version.

This is a **learning project** — every tool is introduced deliberately, one phase at a time, with production-grade patterns. Build iteratively. Do not add infrastructure or abstractions that belong to a later phase.

## Build Phases (The Implementation Order)

Each phase must be fully working before the next begins. The full plan is in `docs/implementation-plan.md` and `docs/sentinel-full-plan.md`.

| Phase | What gets built | Key tools |
|---|---|---|
| 1 | Classifier service (FastAPI + ONNX inference) | FastAPI, ONNX Runtime, Prometheus client, Docker |
| 2 | Model optimization pipeline | HuggingFace Optimum, ONNX, INT8 quantization |
| 3 | Local infra (K8s namespaces, DBs, object storage) | Terraform, Helm, k3d/Minikube, PostgreSQL, MongoDB, MinIO |
| 4 | Observability stack | OTel Collector, Jaeger, Prometheus, Grafana |
| 5 | Trace ingestion + stream processing | Kafka (Strimzi), Python consumer, PostgreSQL writes |
| 6 | Drift detection | PySpark, spark-on-k8s-operator, PSI/JSD metrics |
| 7 | Orchestration + experiment tracking | Apache Airflow, MLflow |
| 8 | Cloud deployment | Terraform workspaces, EKS/GKE, RDS, S3 |

## Key Architectural Decisions

### Data flow
```
Chat app (external)
  → OTel spans (OTLP/gRPC :4317)
  → OTel Collector
  → Kafka topic: traces.raw
  → Stream processor (Python consumer)
  → POST /v1/moderations (classifier service)
  → PostgreSQL: classifications table
  → MongoDB: flagged_content (harmful only, for retraining)
  → Spark batch job: drift metrics → drift_stats table
  → Airflow DAG: triggers retrain when PSI > 0.2
  → Rolling restart: new pods load promoted model from model_registry
```

### OTel span attributes the chat app must emit

Follows the [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) —
the same format used by LangSmith, Arize Phoenix, OpenLLMetry, and
`opentelemetry-instrumentation-openai`.

**Span attributes** (non-sensitive metadata):
```
gen_ai.system                 — provider: "openai", "anthropic", etc.
gen_ai.request.model          — model name: "gpt-4o"
gen_ai.request.max_tokens     — max tokens requested
gen_ai.request.temperature    — sampling temperature
gen_ai.response.model         — actual model that responded
gen_ai.response.finish_reasons— "stop", "length", etc.
gen_ai.usage.input_tokens     — prompt token count
gen_ai.usage.output_tokens    — completion token count
session.id                    — conversation session
```

**Span events** (sensitive content — prompt/response text):
```
Event: gen_ai.content.prompt
  Attribute: gen_ai.prompt     — JSON: [{"role":"user","content":"..."}]

Event: gen_ai.content.completion
  Attribute: gen_ai.completion — JSON: {"role":"assistant","content":"..."}
```

Content travels in span *events*, not attributes. This matches the GenAI spec
and lets collectors redact sensitive content without stripping the span entirely.
The stream processor's `processor.py` extracts text from these events.

### Classifier service design rules
- ONNX Runtime's `session.run()` is a blocking C call — it must never run directly inline in an `async def` route, or it blocks the entire event loop. `/v1/moderations` is the only classifier endpoint (accepts a single string or a list, OpenAI-moderation-compatible) and branches internally: a single-string input goes through `DynamicBatcher` (batches concurrent single-text requests, runs one `predict()` call per batch via `run_in_executor`); a list input is already batched by the caller, so it's dispatched directly via `run_in_executor(None, _classifier.predict, texts)`. The rule is "never block the loop with `session.run()`," not "the route must be `def`" — an `async def` route with the inference call executor-dispatched satisfies it just as well, and the batching gets you adaptive throughput a plain sync route wouldn't. (`/classify` and `/classify/batch` were removed — nothing in the system called them, and `/v1/moderations` covers both single and batch shapes on its own.)
- **No `/reload` endpoint** — with multiple replicas, a reload hits only one pod, causing a silent model version split. Model upgrades always go through `kubectl rollout restart deployment/classifier`.
- Model version comes from `model_registry` table on pod startup (not env var). Env var `MODEL_PATH` is only the fallback when DB is unreachable.
- Prometheus metrics exposed at `/metrics` via `make_asgi_app()` — no separate server needed.

### Model optimization path (in order)
1. PyTorch FP32 baseline (~500 MB, ~110ms p50)
2. ONNX export + O2 graph optimization (~480 MB, ~60ms) — zero accuracy loss
3. ONNX + dynamic INT8 quantization (~120 MB, ~35ms) — <0.2% accuracy loss

ORT session options for single-request workloads: `intra_op_num_threads=4, inter_op_num_threads=1, execution_mode=ORT_SEQUENTIAL`. Flip to `intra=1, inter=N` for concurrent workloads.

### Kafka consumer: at-least-once + idempotent writes
Manual offset commit only **after** successful PostgreSQL write. `ON CONFLICT DO NOTHING` on the classifications table prevents duplicate rows on reprocessing. Topic `traces.raw` has 3 partitions — scaling consumers beyond 3 replicas gives zero benefit.

### Terraform patterns
- Module structure: `variables.tf` (API), `main.tf` (implementation), `outputs.tf` (return values)
- Use `yamlencode({})` for Helm values — cleaner than `set {}` blocks, and sensitive variables mask the entire block in plan output
- `wait = true` on all `helm_release` resources so downstream Terraform resources don't connect to pods that aren't ready yet
- Bitnami PostgreSQL chart: OCI URI (`oci://registry-1.docker.io/bitnamicharts/postgresql`), and SQL init scripts must start with `\connect sentinel;` (initdb runs against `postgres` database by default)
- StatefulSets for databases (PostgreSQL: `postgresql-0`, MongoDB: `mongodb-0`), Deployments for stateless services

### PostgreSQL schema notes
- `TIMESTAMPTZ` everywhere, never `TIMESTAMP`
- `classifications` table indexes: `(ts DESC)`, `(label, ts DESC)`, `(model_version, ts DESC)` — equality columns first, range columns last
- `model_registry.status` CHECK constraint: `IN ('staging', 'active', 'retired')` — DB enforces the invariant, not application code

### Docker image tagging
- K8s Deployment specs always use `sha-<git-sha>` image tags, never `:latest`
- CI builds and validates; CD (push to `main` only) builds and pushes to GHCR

## Namespace Layout (K8s)

```
sentinel-app         — classifier service, stream processor, data simulator
sentinel-data        — PostgreSQL, MongoDB, MinIO, Kafka
sentinel-monitoring  — OTel Collector, Jaeger, Prometheus, Grafana, MLflow
sentinel-pipeline    — Airflow, Spark
```

## Kubernetes Cluster (Local Dev)

k3d or Minikube. Use the `local-path` StorageClass for PVCs.

## Model Registry Source of Truth

`model_registry` PostgreSQL table controls which model version each classifier pod loads. Airflow's `retrain_dag` promotes a new model by updating the table, then runs `kubectl rollout restart deployment/classifier`. Each new pod queries the table on startup and downloads the active ONNX model from MinIO.

## Target Production Folder Structure (Evolve Toward This)

Current phases use `pipelines/optimizer/`, `pipelines/evaluate/` etc. as scaffolding. The end-state production structure organizes by **deployment boundary**, not by "it's ML stuff":

```
sentinel/
  services/
    classifier/        ← long-running FastAPI server (K8s Deployment)
    stream-processor/  ← long-running Kafka consumer (K8s Deployment)
  pipelines/
    optimizer/         ← one-shot ONNX pipeline (K8s Job)
    evaluate/          ← accuracy + latency benchmarks (K8s Job)
    retrain/           ← fine-tuning scripts, called by Airflow
    drift/             ← PySpark PSI/JSD jobs (spark-submit, own pyproject.toml)
  orchestration/       ← Airflow DAG definitions (orchestrates pipelines/)
  datasets/            ← shared data loading utilities
  infra/               ← Terraform + Helm charts
```

Key principle: every folder under `pipelines/` eventually becomes a containerized job with its own Dockerfile and entry point. The distinction between "service" (always running) and "job" (runs to completion) maps directly to K8s `Deployment` vs `Job`. `drift/` gets its own `pyproject.toml` because PySpark manages its own Python environment via `spark-submit --py-files`.

During learning phases, `pipelines/` scripts run locally. Don't force the K8s Job wiring until Phase 6-7.

## Common Interview Points (Do Not Lose These in Refactors)

- Classifier never runs `session.run()` inline in an event loop — always via `DynamicBatcher` (single-string `/v1/moderations` calls) or `run_in_executor` (list `/v1/moderations` calls), never a blocking call directly inside `async def`
- Rolling restart for model upgrades — not in-process reload
- Manual Kafka offset commit after DB write — not auto-commit
- PSI and JSD as the drift metrics (PSI > 0.2 triggers retrain)
- ONNX INT8 dynamic quantization gives ~75% size reduction, ~3x latency, <0.2% accuracy cost
- Spark `.explain()` used to inspect physical plan and find redundant full scans
- Airflow PostgresSensor in `reschedule` mode (not `poke`) to avoid holding worker slots
