# Sentinel — Local Dev Reference

Everything you need to reach, inspect, and test every part of the stack. All Phases 1–7 are deployed; see the root [`README.md`](../README.md) for the phase overview.

---

## Quick Start

```bash
./scripts/dev-start.sh
```

Creates the k3d cluster, builds and imports every service/pipeline image, applies Terraform, waits for every pod to become ready, verifies both Airflow DAGs load with no import errors (and unpauses `retrain_dag`/`drift_dag`), bootstraps a model if `model_registry` is empty, opens every port-forward, and rolling-restarts the classifier + stream processor. Press **Ctrl-C** to stop everything cleanly.

Model loading priority on classifier startup (automatic — no action required in most cases):
1. `model_registry` table → downloads the active (or most recent staging) model from MinIO
2. `MODEL_PATH` env var → loads that directory directly
3. `logs/optimizer/` auto-discovery → uses the most recent local report

To force a specific model:
```bash
MODEL_PATH=/path/to/int8 ./scripts/dev-start.sh
```

---

## Service Map

| Service | Local URL / Address | Credentials |
|---|---|---|
| Classifier API | `http://localhost:8000` | — |
| Classifier docs | `http://localhost:8000/docs` | — |
| Prometheus scrape | `http://localhost:8000/metrics` | — |
| Label UI | `http://localhost:8001` | — |
| MinIO console | `http://localhost:9001` | `sentinel` / `sentinel-minio` |
| MinIO S3 API | `http://localhost:9000` | access: `sentinel` secret: `sentinel-minio` |
| PostgreSQL | `localhost:5432` | `sentinel` / `sentinel` / db `sentinel` |
| MongoDB | `localhost:27017` | `sentinel` / `sentinel` / db `sentinel` |
| mongo-express | `http://localhost:8081` | no login |
| Prometheus | `http://localhost:9090` | — |
| Grafana | `http://localhost:3000` | `admin` / `admin` |
| OTel Collector gRPC | `localhost:4317` | — |
| OTel Collector HTTP | `http://localhost:4318` | — |
| Kafka (EXTERNAL) | `localhost:9094` | — |
| Jaeger UI | `http://localhost:16686` | — |
| Airflow UI | `http://localhost:8090` | `admin` / `sentinel` |
| MLflow UI | `http://localhost:5000` | — |

All of the above run **in-cluster** — there is no host-machine component left in this stack; everything reachable above is via `kubectl port-forward`, opened automatically by `dev-start.sh`.

---

## Classifier

**Base URL:** `http://localhost:8000`

### Health checks
```bash
curl http://localhost:8000/health/live    # process is alive — always 200 once started
curl http://localhost:8000/health/ready   # can it actually serve traffic right now?
```
Liveness and readiness are deliberately separate: liveness should almost never fail (it only signals "the process is fundamentally broken"), while readiness fails during model-load warmup and gracefully during shutdown. See [`services/classifier/explanation.md`](../services/classifier/explanation.md) for why conflating the two causes real outages.

### `/v1/moderations` — the primary endpoint

OpenAI-compatible; this is what the stream processor itself calls, and what any external chat-app integration should call too.

```bash
curl -s -X POST http://localhost:8000/v1/moderations \
  -H "Content-Type: application/json" \
  -d '{"input": ["you are an idiot", "have a nice day"]}' | jq .
```
```json
{
  "id": "modr-...",
  "model": "content-classifier-roberta-20260101T120000Z-int8",
  "results": [
    {"flagged": true,  "categories": {"harm": true},  "category_scores": {"harm": 0.98}},
    {"flagged": false, "categories": {"harm": false}, "category_scores": {"harm": 0.02}}
  ]
}
```

Pass `X-Sentinel-Skip-Persist: true` to skip the classifier's own async PostgreSQL write — used by the stream processor, which persists results itself (with `span_id` for idempotency) instead.

### `/classify` and `/classify/batch` — internal/simple shape

Still available for direct, non-OpenAI-shaped use:
```bash
curl -s -X POST http://localhost:8000/classify \
  -H "Content-Type: application/json" \
  -d '{"text": "you are an idiot"}' | jq .
# {"label":"harm","score":0.9821,"latency_ms":34.2,"model_version":"...","inference_at":"..."}

curl -s -X POST http://localhost:8000/classify/batch \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world", "I will hurt you"]}' | jq .
```

### Prometheus metrics
```bash
curl -s http://localhost:8000/metrics | grep classifier
```

### Interactive API docs
Open `http://localhost:8000/docs`.

---

## Label UI

**URL:** `http://localhost:8001`

The human-in-the-loop review step between the stream processor flagging content and the retraining pipeline consuming it.

- The table lists `flagged_content` documents still awaiting a decision (`training_decision` is `pending` or missing), oldest first, alongside the model's own label/score for context.
- Pick the correct `safe`/`harm` label per row and Accept or Reject it for training.
- **Trigger Retraining** calls Airflow's REST API (`POST /api/v1/dags/retrain_dag/dagRuns`) to start the same DAG the automated drift path uses.

```bash
# Check the current queue depth without opening the browser
curl -s http://localhost:8001/api/stats | jq .

# Trigger a retrain from the CLI
curl -X POST http://localhost:8001/api/trigger-retrain
```

See [`services/label-ui/explanation.md`](../services/label-ui/explanation.md).

---

## MinIO

**Console:** `http://localhost:9001` — login: `sentinel` / `sentinel-minio`
**S3 API:** `http://localhost:9000`

### Manual port-forward (if dev-start.sh is not running)
```bash
kubectl port-forward -n sentinel-data svc/minio 9000:9000 9001:9001 &
```

### List buckets from inside the cluster
```bash
kubectl run mc-check --rm -it --restart=Never \
  -n sentinel-data \
  --image=minio/mc:RELEASE.2024-11-21T17-21-54Z \
  --command -- /bin/sh -c \
  'mc alias set minio http://minio:9000 sentinel sentinel-minio && mc ls minio/'
```
Expected output:
```
[date]     0B datasets/
[date]     0B models/
[date]     0B mlflow/
```

### Object layout per optimizer/retraining run
```
models/
  <run-id>/
    fp32/                      — exported ONNX + tokenizer files
    o2/                        — O2 graph-optimized ONNX + tokenizer files
    int8/                      — INT8 quantized ONNX + tokenizer files (loaded by the classifier)
    report.json                — full pipeline report (stage durations, minio paths, timestamps)
    benchmark_report.json      — accuracy/F1/etc. for this run, used as a future retrain's regression baseline
```

The classifier downloads the `int8/` directory from MinIO on startup and caches it locally at `/tmp/sentinel-model-cache/<run-id>/int8/`. `mlflow/` holds MLflow's own artifact store (see the MLflow section below); it isn't meant to be browsed directly.

---

## Prometheus

**URL:** `http://localhost:9090`

Scrapes the classifier at `classifier.sentinel-app.svc.cluster.local:8000` every 10 seconds — the in-cluster Service DNS name, since the classifier itself now runs as a K8s Deployment (no `host.k3d.internal` workaround needed).

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090 &
```

### Useful queries
```
rate(classifier_requests_total[5m])                                            # request rate by label
histogram_quantile(0.95, rate(classifier_request_latency_seconds_bucket[5m]))   # P95 latency
rate(classifier_batch_size_bucket[5m])                                          # batch size distribution
```

Check scrape health at `http://localhost:9090/targets` — the `classifier` job should show `State: UP`.

---

## Grafana

**URL:** `http://localhost:3000` — login: `admin` / `admin`

The Prometheus datasource is auto-provisioned via ConfigMap on first boot.

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000 &
```

Note: the browser-side datasource health check can show a cosmetic "Failed to fetch" warning (the browser can't resolve in-cluster DNS) — actual queries route through the Grafana backend proxy and work correctly.

---

## PostgreSQL

**Address:** `localhost:5432`
**Connection string:** `postgresql://sentinel:sentinel@localhost:5432/sentinel`

### Manual port-forward
```bash
kubectl port-forward -n sentinel-data svc/postgresql 5432:5432 &
```

### Key tables

#### `model_registry`
Tracks every model version and which one should be serving.

```sql
SELECT model_version, model_path, threshold, status, created_at, promoted_at
FROM model_registry
ORDER BY created_at DESC;
```

| Column | Type | Notes |
|---|---|---|
| `model_version` | VARCHAR UNIQUE | e.g. `content-classifier-roberta-20260101T120000Z-int8`, or a run UUID from `pipelines/optimizer` |
| `model_path` | TEXT | MinIO key `models/<run-id>/int8/model_quantized.onnx`, or an absolute local path if MinIO was unavailable when that run finished |
| `threshold` | FLOAT | Default 0.5 — score above this → `harm`. Stored per model version, but **not currently read back into the running classifier** (see `ISSUES.md` #8) — the classifier always uses the `CLASSIFY_THRESHOLD` env var |
| `status` | VARCHAR | `staging` → `active` → `retired`, `CHECK` constraint enforced |
| `created_at` | TIMESTAMPTZ | |
| `promoted_at` | TIMESTAMPTZ | Set by `orchestration/retrain_dag.py`'s `decide_promotion` task |

Every pipeline run (`pipelines/optimizer`, whether invoked directly or via `pipelines/retraining`) registers as `staging`. Promotion to `active` only happens in `retrain_dag.py`, and only if the quality gate passes.

To manually promote a model for testing:
```sql
UPDATE model_registry SET status = 'retired' WHERE status = 'active';
UPDATE model_registry SET status = 'active', promoted_at = NOW() WHERE model_version = '<version>';
```
Then `kubectl rollout restart deployment/classifier deployment/stream-processor -n sentinel-app`.

#### `classifications`
Every inference result. Written by the stream processor (synchronously, for real traffic) and — unless `X-Sentinel-Skip-Persist` is set — the classifier's own async write path.

```sql
SELECT ts, label, score, model_version, latency_ms FROM classifications ORDER BY ts DESC LIMIT 20;
SELECT label, COUNT(*), ROUND(AVG(score)::numeric, 4) AS avg_score FROM classifications GROUP BY label;
```

| Column | Type | Notes |
|---|---|---|
| `span_id` | TEXT | OTLP span ID — nullable (the classifier's own async writes have no span) |
| `text_type` | VARCHAR(8) | `"prompt"` or `"response"` |

`(span_id, text_type)` has a partial unique index (`WHERE span_id IS NOT NULL`) — `ON CONFLICT DO NOTHING` on it is what makes Kafka redelivery idempotent.

#### `drift_stats`
Written by `pipelines/drift/drift_job.py`, one row per completed comparison. `orchestration/drift_dag.py` reads the latest row here (not the Spark job's own K8s exit status) to decide whether to trigger a retrain.

```sql
SELECT model_version, psi, jsd, drift_flagged, computed_at
FROM drift_stats ORDER BY computed_at DESC LIMIT 5;
```

| Column | Type | Notes |
|---|---|---|
| `psi` | FLOAT | Population Stability Index vs. a reference window — `> 0.2` sets `drift_flagged` |
| `jsd` | FLOAT | Jensen-Shannon Divergence, recorded for visibility, not currently gating anything |
| `n_samples` | INT | Size of the current window being compared |

---

## MongoDB

**Address:** `localhost:27017`
**Connection URI:** `mongodb://sentinel:sentinel@localhost:27017/sentinel`

### Manual port-forward
```bash
kubectl port-forward -n sentinel-data svc/mongodb 27017:27017 &
```

### Key collections

#### `flagged_content`
Every `harm` classification plus a `SAFE_SAMPLE_RATE` (default 10%) sample of `safe` ones — written by the stream processor, reviewed in `services/label-ui`, consumed as training data by `pipelines/retraining`.

```js
db.flagged_content.find().sort({ ts: -1 }).limit(10)
db.flagged_content.aggregate([{ $group: { _id: "$training_decision", count: { $sum: 1 } } }])
```

Document shape:
```json
{
  "ts": "ISODate(...)",
  "input_text": "...",
  "text_type": "prompt or response",
  "label": "harm or safe (the MODEL's own classification)",
  "score": 0.98,
  "model_version": "...",
  "session_id": "...",
  "span_id": "...",
  "trace_id": "...",
  "llm_model": "gpt-4o (which LLM the chat app called)",
  "manual_label": "safe | harm | null — set only by services/label-ui",
  "training_decision": "pending | accepted | rejected — defaults to pending"
}
```

`manual_label`/`training_decision` are written with `$setOnInsert`, not `$set`, on upsert — a Kafka redelivery of an already-labelled span updates the ingestion fields but can never clobber a completed manual label.

---

## mongo-express

**URL:** `http://localhost:8081` (no login — local dev only)

```bash
kubectl port-forward -n sentinel-data svc/mongo-express 8081:8081 &
```

Navigate to the `sentinel` database → `flagged_content` to inspect labelling state visually.

---

## Optimizer Pipeline

Runs locally or as a K8s pod (`pipelines/retraining` invokes it in-process). Exports a model to ONNX, applies O2 + INT8 quantization, uploads every stage plus a benchmark report to MinIO, and registers the result in `model_registry` as `staging`.

### Prerequisites
- MinIO port-forward on 9000 (or set `MINIO_ENDPOINT`)
- PostgreSQL port-forward on 5432 (or set `DATABASE_URL`)

### Run
```bash
uv run --package sentinel-optimizer python -m pipelines.optimizer \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir artifacts
```

`--model-id` accepts a HuggingFace Hub id **or a local directory** — this is how `pipelines/retraining` plugs a freshly fine-tuned checkpoint into this exact pipeline unchanged.

### Environment variable overrides
```bash
MINIO_ENDPOINT=http://localhost:9000
MINIO_ACCESS_KEY=sentinel
MINIO_SECRET_KEY=sentinel-minio
MINIO_BUCKET=models
DATABASE_URL=postgresql://sentinel:sentinel@localhost:5432/sentinel
```

If MinIO is unavailable when a run finishes, the local artifact path is registered in `model_registry` instead of a MinIO key — `orchestration/retrain_dag.py`'s `decide_promotion` refuses to promote such a run, since that path only exists inside the (already-deleted) pod that produced it.

---

## Evaluation Pipeline

Scores a candidate model against the held-out set in `datasets/test_dataset.csv` (3780 rows, 9 risk categories) and gates promotion.

```bash
uv run --package sentinel-evaluation python -m pipelines.evaluation.benchmark \
  --model-dir logs/optimizer/<run-id>/int8 \
  --output logs/evaluation/<run-id>/benchmark_report.json

uv run --package sentinel-evaluation python -m pipelines.evaluation.validate \
  --candidate logs/evaluation/<run-id>/benchmark_report.json \
  --baseline  logs/evaluation/<active-run-id>/benchmark_report.json   # optional
```

`validate.py` fails (non-zero exit) if accuracy is below `MIN_ACCURACY` (0.85), or — when a baseline is given — if accuracy dropped more than `MAX_ACCURACY_DROP` (0.01) relative to it. `pipelines/retraining/pipeline.py` calls both automatically and supplies the currently-active model's own stored benchmark report as the baseline.

---

## Retraining Pipeline

Fine-tunes on manually-labelled data, logs the run to MLflow, then hands off to the optimizer/evaluation pipelines above unchanged.

```bash
uv run --package sentinel-retraining python -m pipelines.retraining \
  --output-dir artifacts --log-dir logs --epochs 3
```

Needs `MONGO_URI`, `DATABASE_URL`, MinIO env vars, and `MLFLOW_TRACKING_URI` set (all pre-wired when run via `orchestration/retrain_dag.py`'s `KubernetesPodOperator`). Raises a clear error — not promoted, not a crash — if `flagged_content` has fewer than 2 accepted, manually-labelled documents.

See [`pipelines/retraining/explanation.md`](../pipelines/retraining/explanation.md) for the training loop, the MLflow metrics logged per epoch, and the live-debugged OOM/thread-thrashing issues found building it.

---

## Drift Detection

A one-shot PySpark job, run automatically every hour by `orchestration/drift_dag.py`, or manually:

```bash
kubectl apply -f pipelines/drift/spark-application.yaml
kubectl get sparkapplication -n sentinel-pipeline
kubectl logs -n sentinel-pipeline sentinel-drift-driver
```

Compares the current model's recent classification scores against a reference window, computes PSI/JSD, and writes a row to `drift_stats`. See [`pipelines/drift/explanation.md`](../pipelines/drift/explanation.md) for the metrics math and the `get_active_model_version` gotcha (why it deliberately doesn't just query `status = 'active'`).

---

## Airflow

**URL:** `http://localhost:8090` — login: `admin` / `sentinel`

```bash
kubectl port-forward -n sentinel-pipeline svc/airflow-webserver 8090:8080 &
```
(Local port 8090, not 8080 — k3d's own `serverlb` container already publishes host port 8080.)

Three DAGs:

| DAG | Schedule | What it does |
|---|---|---|
| `healthcheck` | manual | Smoke-tests that the deployment mechanism itself works |
| `retrain_dag` | manual (via Label UI, or `airflow dags trigger retrain_dag`) | Runs the retraining pod → gates on quality → promotes + rolls out on success |
| `drift_dag` | hourly | Runs the drift job → triggers `retrain_dag` automatically if `drift_flagged` |

```bash
# Check DAG parse health
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- airflow dags list-import-errors

# Trigger and watch a run
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- airflow dags trigger retrain_dag
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- airflow dags list-runs -d retrain_dag
```

See [`orchestration/explanation.md`](../orchestration/explanation.md) for both DAGs' task-by-task design and the several live-debugged Kubernetes/Airflow-integration gotchas found building them (subPath ConfigMap mounts not live-updating, `SparkKubernetesOperator`'s naming/labels/deletion quirks, etc.).

---

## MLflow

**URL:** `http://localhost:5000`

```bash
kubectl port-forward -n sentinel-monitoring svc/mlflow 5000:5000 &
```

Backend store: a dedicated `mlflow` database on the same PostgreSQL instance. Artifact store: the `mlflow` MinIO bucket. Every `pipelines/retraining` run logs to the `sentinel-retraining` experiment — params (base model, epochs, batch size, dataset sizes/sources), per-epoch train/eval loss/accuracy/precision/recall/F1, and final summary metrics.

---

## Kubernetes — Inspect the Cluster

```bash
kubectl get pods -n sentinel-data
kubectl get pods -n sentinel-app
kubectl get pods -n sentinel-monitoring
kubectl get pods -n sentinel-pipeline

kubectl logs -n sentinel-app deployment/classifier
kubectl logs -n sentinel-app deployment/stream-processor
kubectl logs -n sentinel-app deployment/label-ui
kubectl logs -n sentinel-pipeline airflow-scheduler-0 -c scheduler
```

---

## Port-Forward Reference (manual)

```bash
kubectl port-forward -n sentinel-data svc/postgresql 5432:5432 &
kubectl port-forward -n sentinel-data svc/mongodb 27017:27017 &
kubectl port-forward -n sentinel-data svc/minio 9000:9000 9001:9001 &
kubectl port-forward -n sentinel-data svc/mongo-express 8081:8081 &
kubectl port-forward -n sentinel-data svc/kafka 9094:9094 &
kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090 &
kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000 &
kubectl port-forward -n sentinel-monitoring svc/jaeger 16686:16686 &
kubectl port-forward -n sentinel-monitoring svc/otel-collector 4317:4317 4318:4318 &
kubectl port-forward -n sentinel-monitoring svc/mlflow 5000:5000 &
kubectl port-forward -n sentinel-pipeline svc/airflow-webserver 8090:8080 &
kubectl port-forward -n sentinel-app svc/classifier 8000:8000 &
kubectl port-forward -n sentinel-app svc/label-ui 8001:8001 &
```

---

## OTel Collector

**gRPC:** `localhost:4317`  **HTTP:** `http://localhost:4318`

Fans OTLP traces out to Kafka topic `traces.raw` (3 partitions, `otlp_json` encoding) and Jaeger.

```bash
python scripts/simulate-traces.py --count 10 --harm-pct 0.3
```

---

## Jaeger

**URL:** `http://localhost:16686`

In-memory (local dev only — traces don't survive restarts). Search by service name `chat-app-simulator` after running `simulate-traces.py`.

---

## Stream Processor

Kafka consumer → classify → PostgreSQL + MongoDB. Runs as a K8s Deployment.

```bash
kubectl logs -f -n sentinel-app deployment/stream-processor
```

### Data flow
```
Kafka traces.raw
  → extract LLM spans (prompt + response)
  → POST /v1/moderations (X-Sentinel-Skip-Persist: true), chunked to CLASSIFY_CHUNK_SIZE
  → write to PostgreSQL classifications  (all, idempotent on span_id+text_type)
  → write to MongoDB flagged_content     (harm + SAFE_SAMPLE_RATE sample of safe, idempotent upsert)
  → commit Kafka offset — only after both writes succeed
```

### Verify it's working
```bash
python scripts/simulate-traces.py --count 10 --harm-pct 0.5

psql postgresql://sentinel:sentinel@localhost:5432/sentinel \
  -c "SELECT label, count(*) FROM classifications GROUP BY label;"

mongosh "mongodb://sentinel:sentinel@localhost:27017/sentinel" \
  --eval "db.flagged_content.find().sort({ts:-1}).limit(5)"
```

### Key design decisions
- Calls the **same OpenAI-compatible `/v1/moderations` endpoint** an external caller would, rather than an internal-only shape — see [`services/stream-processor/explanation.md`](../services/stream-processor/explanation.md) for why, and the mid-build story of this decision being accidentally reverted and caught.
- Manual Kafka offset commit, after both PostgreSQL *and* MongoDB writes succeed.
- Reconnects on a dropped PostgreSQL connection, rolls back on a poisoned transaction (a stale `model_version` foreign-key violation) — without the rollback, every subsequent batch would fail forever on the same connection.
- `SAFE_SAMPLE_RATE=0.1` (10%) keeps the retraining dataset from being 100% harmful content.
- `traces.raw` has 3 partitions — scaling beyond 3 replicas gives no benefit.

---

## What's Next (Phase 8)

Cloud deployment — Terraform workspaces, EKS/GKE, RDS, S3 — is the only phase not yet built. See the root [`README.md`](../README.md)'s build-phases table and [`CLAUDE.md`](../CLAUDE.md) for the plan.
