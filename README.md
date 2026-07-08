<p align="center">
  <img src="assets/sentinel-logo.png" alt="Sentinel logo" width="160">
</p>

# Sentinel

A content safety monitoring platform for LLM applications. Sentinel receives OpenTelemetry traces from a chat app, classifies prompt/response text for toxicity, detects when the model's input distribution drifts, and automatically retrains and redeploys a new model version — with a human-in-the-loop labelling step in between.

Built as a learning project — every tool is introduced one phase at a time with production-grade patterns. All 7 local-dev phases below are complete; only cloud deployment (Phase 8) remains.

<p align="center">
  <img src="assets/sentinel-video.gif" alt="Sentinel demo" width="720">
  <br>
  <sub>Full video: <a href="assets/sentinel-video.mp4">assets/sentinel-video.mp4</a></sub>
</p>

## What it does

```
Chat app (external, or scripts/simulate-traces.py)
  → OTel spans (OTLP/gRPC :4317)
  → OTel Collector
  → Kafka topic: traces.raw
  → Stream processor
  → POST /v1/moderations (classifier service, OpenAI-compatible endpoint)
  → PostgreSQL: classifications table
  → MongoDB: flagged_content (harmful + a sample of safe, for retraining)

  → services/label-ui: a human reviews flagged_content, assigns a
    ground-truth label, and accepts/rejects it for training

  → orchestration/drift_dag.py (hourly): runs a PySpark job comparing
    recent scores against a reference window (PSI/JSD)
      ├─ no drift → nothing happens
      └─ PSI > 0.2 → triggers orchestration/retrain_dag.py automatically
         (a human can also trigger this directly from the labelling UI)

  → pipelines/retraining: fine-tunes on the accepted labels, logs the run
    to MLflow, then hands off to the *same* optimizer + evaluation
    pipelines a manual run would use (export → ONNX → INT8 → benchmark)

  → orchestration/retrain_dag.py: promotes the new model only if it beats
    the quality gate (an absolute accuracy floor + no regression vs. the
    model currently in production), then rolling-restarts the classifier
    and stream processor to pick it up
```

## Project structure

```
sentinel/
  services/
    classifier/         — FastAPI + ONNX Runtime inference, OpenAI-compatible /v1/moderations
    stream-processor/    — Kafka consumer: classify traces, write to PostgreSQL + MongoDB
    label-ui/            — manual labelling UI for flagged_content, triggers retraining
  pipelines/
    optimizer/           — ONNX export + O2 graph optimization + INT8 quantization
    evaluation/           — accuracy/F1/AUC-ROC benchmarks + promotion quality gate
    drift/                — PySpark PSI/JSD drift detection job
    retraining/           — fine-tunes on labelled data, logs to MLflow, reuses optimizer+evaluation
  orchestration/          — Airflow DAGs (healthcheck, retrain_dag, drift_dag)
  infra/
    terraform/local/      — the entire local k3d stack, declared in Terraform
    mlflow/, prometheus/, grafana/ — Dockerfiles and config for those services
  datasets/               — held-out evaluation set (test_dataset.csv) + loader
  scripts/                — dev-start.sh (full stack bring-up), simulate-traces.py (quick smoke test),
                            simulate-traces-prod.py (production-shaped load: 1000s of spans/sec, drift ramp)
  docs/                   — local-dev reference (see note below — due for a refresh)
  tests/                  — classifier API tests (mocked, no model/cluster needed)
```

Every directory above has its own `explanation.md` with a deep walkthrough of the code, the design decisions, and — where relevant — the actual live-debugged bugs and gotchas encountered building it. Start there for anything beyond a surface read of the source.

## Build phases

| Phase | What gets built | Status |
|-------|----------------|--------|
| 1 | Classifier service — FastAPI + ONNX inference + Prometheus metrics | Complete ✓ |
| 2 | Model optimization pipeline — ONNX export, O2 graph opt, INT8 quantization | Complete ✓ |
| 3 | Local infra — k3d, PostgreSQL, MongoDB, MinIO via Terraform | Complete ✓ |
| 4 | Observability — Prometheus, Grafana, Jaeger, OTel Collector | Complete ✓ |
| 5 | Trace ingestion — OTel Collector, Kafka consumer, PostgreSQL + MongoDB writes | Complete ✓ |
| 6 | Drift detection — PySpark, PSI/JSD metrics | Complete ✓ |
| 7 | Orchestration — Airflow DAGs, MLflow, manual labelling UI, automated retraining | Complete ✓ |
| 8 | Cloud deployment — EKS/GKE, RDS, S3 via Terraform workspaces | Pending |

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (or a compatible container runtime)
- [k3d](https://k3d.io/) — lightweight local Kubernetes
- `kubectl`
- [Terraform](https://developer.hashicorp.com/terraform/downloads) ≥ 1.6
- [uv](https://docs.astral.sh/uv/) — Python package/dependency manager
- Python 3.12 (uv will manage this for you if it isn't already installed)

## Quickstart

```bash
git clone https://github.com/VjayRam/project-sentinel.git
cd project-sentinel
uv sync --all-packages    # installs the root workspace + classifier deps for local tooling

./scripts/dev-start.sh
```

`dev-start.sh` does everything, in order:

1. Creates (or starts) the k3d cluster
2. Builds and imports every service/pipeline image (classifier, stream-processor, drift, mlflow, label-ui, retraining) into k3d
3. Runs `terraform apply` — deploys every namespace, database, and service declared in `infra/terraform/local/`
4. Waits for all data-layer and pipeline pods to pass readiness checks
5. Verifies the Kafka topic exists and every Airflow DAG loads with no import errors, and unpauses `retrain_dag`/`drift_dag`
6. Syncs the PostgreSQL password and bootstraps a model into `model_registry` if none exists yet (runs the optimizer pipeline locally — downloads ~500MB on first run)
7. Opens port-forwards for every service
8. Rolling-restarts the classifier and stream processor so they pick up the bootstrapped model

Press **Ctrl-C** to stop everything cleanly (port-forwards only — the k3d cluster itself keeps running; re-run the script to reconnect).

## Service map

Once `dev-start.sh` is running, everything is reachable on `localhost`:

| Service | URL | Credentials |
|---|---|---|
| Classifier API | `http://localhost:8000` | — |
| Classifier docs (Swagger) | `http://localhost:8000/docs` | — |
| Classifier metrics | `http://localhost:8000/metrics` | — |
| Label UI | `http://localhost:8001` | — |
| Airflow UI | `http://localhost:8090` | `admin` / `sentinel` |
| MLflow UI | `http://localhost:5000` | — |
| Jaeger UI | `http://localhost:16686` | — |
| Grafana | `http://localhost:3000` | `admin` / `admin` |
| Prometheus | `http://localhost:9090` | — |
| MinIO console | `http://localhost:9001` | `sentinel` / `sentinel-minio` |
| MinIO S3 API | `http://localhost:9000` | access key `sentinel` / secret `sentinel-minio` |
| mongo-express | `http://localhost:8081` | — |
| OTel Collector | `grpc://localhost:4317`, `http://localhost:4318` | — |
| Kafka (external listener) | `localhost:9094` | — |
| PostgreSQL | `localhost:5432` | `sentinel` / `sentinel`, db `sentinel` |
| MongoDB | `localhost:27017` | `sentinel` / `sentinel`, db `sentinel` |

## Key workflows

### Send test traffic

```bash
python scripts/simulate-traces.py --count 20 --harm-pct 0.3
```

Emits synthetic OTLP traces through the same collector path a real chat app would use. Watch them land end-to-end:

For production-scale load — thousands of spans in seconds, weighted model mix, multi-turn sessions, realistic latency, and an optional ramping-harm-pct mode to actually exercise drift detection — use `simulate-traces-prod.py` instead:

```bash
python scripts/simulate-traces-prod.py --count 5000 --rps 400 --concurrency 100
python scripts/simulate-traces-prod.py --duration 60 --rps 50 --pattern diurnal
python scripts/simulate-traces-prod.py --count 3000 --drift-harm 0.05:0.6
```

```bash
# Classifications in Postgres
psql postgresql://sentinel:sentinel@localhost:5432/sentinel \
  -c "SELECT label, count(*) FROM classifications GROUP BY label;"

# Flagged content in MongoDB
mongosh "mongodb://sentinel:sentinel@localhost:27017/sentinel" \
  --eval "db.flagged_content.find().sort({ts:-1}).limit(5)"

# The full trace in Jaeger
open http://localhost:16686   # search service: chat-app-simulator
```

### Label flagged content and trigger a retrain manually

1. Open the Label UI (`http://localhost:8001`) — it lists `flagged_content` documents awaiting review, alongside the model's own label/score for context.
2. For each one, pick the correct `safe`/`harm` label and Accept or Reject it for training.
3. Click **Trigger Retraining** — this calls Airflow's REST API to start `retrain_dag`, the same DAG the automated drift path uses.
4. Watch progress in the Airflow UI (`http://localhost:8090`) and the run's metrics in MLflow (`http://localhost:5000`).

If the retrained model passes the quality gate (an absolute accuracy floor, and — once there's a previously-promoted model to compare against — no meaningful regression vs. it), `retrain_dag` promotes it to `active` in `model_registry` and rolling-restarts the classifier and stream processor.

### Let drift trigger a retrain automatically

No action needed — `drift_dag` runs hourly on its own, submits the drift Spark job, and calls `retrain_dag` itself if PSI exceeds 0.2. It needs the same accepted-labels data as the manual path; if nobody has labelled anything yet, the automated run reports a clean "not enough data" result rather than promoting anything.

### Run the model optimizer directly

Useful if you want to bootstrap or replace the model outside of a full retrain cycle:

```bash
uv run --package sentinel-optimizer python -m pipelines.optimizer \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir artifacts
```

Exports the HF model to ONNX, applies O2 graph optimization, quantizes to INT8, uploads every stage to MinIO, and registers the result in `model_registry` as `staging`.

## Model optimization pipeline

Converts a fine-tuned RoBERTa classifier from HuggingFace into a production-ready ONNX INT8 model.

| Stage | Input | Output | Size | Notes |
|-------|-------|--------|------|-------|
| Export | HF Hub model (or a local fine-tuned checkpoint) | `model.onnx` | ~500 MB | FP32 |
| Optimize | `model.onnx` | `model_optimized.onnx` | ~480 MB | O2 graph fusions, zero accuracy loss |
| Quantize | `model_optimized.onnx` | `model_quantized.onnx` | ~120 MB | Dynamic INT8, <0.2% accuracy loss |

Each run gets a UUID and is written both locally and to MinIO:

```
artifacts/<run-id>/{fp32,o2,int8}/       — local (gitignored)
logs/optimizer/<run-id>/report.json       — local (gitignored)

MinIO models/<run-id>/{fp32,o2,int8}/     — survives pod termination
MinIO models/<run-id>/report.json         — full pipeline report
MinIO models/<run-id>/benchmark_report.json — used as a regression baseline by future retrains
```

See [`pipelines/optimizer/explanation.md`](pipelines/optimizer/explanation.md) for a detailed walkthrough of every stage.

## Model lifecycle

```
pipelines/optimizer (called directly, or via pipelines/retraining)
  → uploads fp32/o2/int8 artifacts + benchmark report to MinIO
  → writes a model_registry row (status = staging)

classifier pod startup
  → queries model_registry for the active model (falls back to most-recent staging)
  → downloads int8 artifacts from MinIO to /tmp/sentinel-model-cache/<run-id>/int8/
  → loads the ONNX model + tokenizer
  → self-registers in model_registry (idempotent)

orchestration/retrain_dag.py (via label-ui manually, or drift_dag automatically)
  → runs the fine-tune + optimize + evaluate cycle
  → promotes staging → active only if the quality gate passes
  → patches classifier + stream-processor Deployments to roll out the new model
```

`model_registry.status` (`staging` → `active` → `retired`) is the single source of truth for which model should be serving — see [`docs/local-dev.md`](docs/local-dev.md) for the schema, and [`orchestration/explanation.md`](orchestration/explanation.md) for exactly how promotion decisions are made.

Model upgrades always go through a rolling restart — there's no `/reload` endpoint. With multiple replicas, an in-process reload would hit only one pod, causing a silent model-version split across the fleet.

## Key design decisions

- **Sync dispatch for ONNX inference** — `session.run()` is a blocking C call; it's never invoked directly inside `async def` route code. Concurrent single-text requests batch through a `DynamicBatcher`; batch/moderation routes dispatch via `run_in_executor`.
- **Rolling restart for model upgrades** — no `/reload` endpoint, for the reason above.
- **`model_registry` as source of truth** — the classifier queries Postgres on startup to find its model; `MODEL_PATH` is only a fallback for when the DB is unreachable.
- **Manual Kafka offset commit** — committed only after a successful PostgreSQL *and* MongoDB write; both are idempotent on redelivery (`ON CONFLICT DO NOTHING` / upsert keyed on `span_id, text_type`).
- **PSI > 0.2 triggers retrain** — Population Stability Index and Jensen-Shannon Divergence as drift metrics, computed in PySpark against a reference window.
- **Dynamic INT8 quantization** — weights quantized offline, activations at runtime, no calibration dataset needed; ~75% size reduction, ~3x latency improvement, <0.2% accuracy cost.
- **Promotion requires a human-labelled ground truth** — the model's own predictions are never used to train itself; `flagged_content` is reviewed and labelled by an operator in `services/label-ui` before it becomes training data.
- **Automated retrains are still quality-gated** — a drift-triggered retrain that regresses accuracy relative to the currently active model is rejected before promotion, the same as a manually-triggered one.

## Load testing

The classifier was load- and stress-tested against its two `/v1/moderations` dispatch paths — single-string input (queued through `DynamicBatcher`) and list input (dispatched directly via `run_in_executor`) — using a custom async Python harness (httpx + asyncio, no external tool needed), ramping concurrency per stage until latency/error degradation was observed. Pod resource usage (CPU/memory) was sampled concurrently via `kubectl top pod`.

**Setup:** 1 classifier replica, resource limits `cpu: 1000m` / `memory: 1Gi` (k3d, local).

**Baseline (`ORT_INTRA_THREADS=4`, the single-request-tuned default):**

| Path | Concurrency | Throughput | p50 | p99 |
|---|---|---|---|---|
| Single-string | 1 → 10 | 13.1 → 15.3 req/s (flat) | 94.7ms → 641.8ms | 101.7ms → 899.7ms |
| List (32-item batches) | 1 → 5 | 17.0 → 19.5 items/s (flat) | 1.9s → 8.2s | 2.0s → 10.4s |

Throughput plateaued while latency kept climbing — a queueing/contention signature, not a request failure (0% errors throughout). Root cause: the pod's CPU **limit** (1 core) is *below* `ORT_INTRA_THREADS`'s thread count (4), so every request — concurrent or not — pays Kubernetes CFS quota-throttling overhead rather than getting real 4-way parallelism. CPU peaked at 1011m (at the 1000m limit) and memory peaked at 1019Mi (near the 1024Mi limit) during the worst stages.

**Fix and re-test (`ORT_INTRA_THREADS=1`):** applied as a Terraform env var change (`infra/terraform/local/main.tf`), rebuilt, rolled out, and re-tested identically:

| Path | Concurrency | Throughput vs. baseline | p50 vs. baseline |
|---|---|---|---|
| Single-string | 1 | 26.1 req/s (**+99%**) | 36.9ms (**-61%**) |
| Single-string | 10 | 36.3 req/s (**+137%**) | 284.3ms (**-56%**) |
| List (32-item batches) | 1 | 32.6 items/s (**+92%**) | 960.6ms (**-49%**) |
| List (32-item batches) | 5 | 23.4 items/s (**+20%**) | 6.7s (**-18%**) |

Not a tradeoff — `intra=1` won at every concurrency level on both paths, including concurrency=1 with zero contention. Average CPU utilization during the test nearly tripled (212m → 545m), confirming the mechanism: under `intra=4`, most of the CPU quota was being burned on thread-scheduling overhead rather than inference; `intra=1` has no thread/quota mismatch to throttle. Peak memory was essentially unchanged (~1017-1019Mi either way) — the near-OOM risk is a separate, still-open issue (batch tensor size), not something this fix addresses.

Full methodology, per-stage percentile tables, and resume-ready framing: [`docs/load-test-report.md`](docs/load-test-report.md) (baseline) and [`docs/load-test-report-intra1.md`](docs/load-test-report-intra1.md) (fix + comparison).

## Tech stack

| Concern | Tool |
|---------|------|
| Inference API | FastAPI, ONNX Runtime |
| Model optimization | HuggingFace Optimum, ONNX Runtime |
| Fine-tuning | HuggingFace Transformers |
| Stream processing | Kafka (KRaft mode), Python consumer |
| Databases | PostgreSQL (classifications, model registry, drift stats), MongoDB (flagged content) |
| Object storage | MinIO |
| Drift detection | PySpark, PSI/JSD |
| Orchestration | Apache Airflow (LocalExecutor) |
| Experiment tracking | MLflow |
| Manual labelling | FastAPI + plain HTML/JS (`services/label-ui`) |
| Observability | OTel Collector, Prometheus, Grafana, Jaeger |
| Infrastructure | Terraform, k3d |
| CI/CD | GitHub Actions, GHCR |

## Where to learn more

- [`CLAUDE.md`](CLAUDE.md) — architectural decisions, phase plan, and the interview-relevant points this project deliberately keeps intact across refactors.
- Every directory listed under **Project structure** above has its own `explanation.md` — the deepest, most accurate source for how any given piece works and why.
- [`docs/local-dev.md`](docs/local-dev.md) — a granular service-by-service reference (ports, sample queries, manual port-forward commands). **Note:** written during Phase 5 and not yet updated for Phases 6–7 (drift/orchestration/MLflow/label-ui) — treat the per-service sections for Phases 1–5 as accurate and the rest as pending a refresh.
- [`ISSUES.md`](ISSUES.md) — a production-readiness audit; note it also predates some of the fixes already made (e.g. the classifier Dockerfile it flags as missing exists now) — read it as a snapshot in time, not a current gap list.
- [`docs/load-test-report.md`](docs/load-test-report.md) and [`docs/load-test-report-intra1.md`](docs/load-test-report-intra1.md) — classifier load/stress test methodology, results, and a measured before/after on an ORT thread-tuning fix.
