# Sentinel — Local Dev Reference

Everything you need to reach, inspect, and test every part of the stack.

---

## Quick Start

```bash
./scripts/dev-start.sh
```

Starts k3d, applies Terraform, opens port-forwards, and launches the classifier.
Press **Ctrl-C** to stop everything cleanly.

Model loading priority (automatic — no action required in most cases):
1. `model_registry` table → downloads active/staging model from MinIO
2. `MODEL_PATH` env var → loads that directory directly
3. `logs/optimizer/` auto-discovery → uses the most recent local report

To force a specific model:
```bash
MODEL_PATH=/path/to/int8 ./scripts/dev-start.sh
```

---

## Service Map

| Service | Local URL / Address | Credentials | Status |
|---|---|---|---|
| Classifier API | `http://localhost:8000` | — | Phase 1 ✓ |
| Classifier docs | `http://localhost:8000/docs` | — | Phase 1 ✓ |
| Prometheus scrape | `http://localhost:8000/metrics/` | — | Phase 1 ✓ |
| MinIO console | `http://localhost:9001` | `sentinel` / `sentinel-minio` | Phase 3 ✓ |
| MinIO S3 API | `http://localhost:9000` | access: `sentinel` secret: `sentinel-minio` | Phase 3 ✓ |
| PostgreSQL | `localhost:5432` | `sentinel` / `sentinel` / db `sentinel` | Phase 3 ✓ |
| MongoDB | `localhost:27017` | `sentinel` / `sentinel` / db `sentinel` | Phase 3 ✓ |
| mongo-express | `http://localhost:8081` | no login | Phase 3 ✓ |
| Prometheus | `http://localhost:9090` | — | Phase 4 ✓ |
| Grafana | `http://localhost:3000` | `admin` / `admin` | Phase 4 ✓ |
| OTel Collector gRPC | `localhost:4317` | — | Phase 5 ✓ |
| OTel Collector HTTP | `http://localhost:4318` | — | Phase 5 ✓ |
| Kafka (EXTERNAL) | `localhost:9094` | — | Phase 5 ✓ |
| Jaeger UI | `http://localhost:16686` | — | Phase 5 ✓ |

---

## Classifier

**Base URL:** `http://localhost:8000`

### Health check
```bash
curl http://localhost:8000/health
# {"status":"ok","model":"VijayRam1812/content-classifier-roberta"}
```

### Single classification
```bash
curl -s -X POST http://localhost:8000/classify \
  -H "Content-Type: application/json" \
  -d '{"text": "you are an idiot"}' | jq .
```
```json
{
  "label": "harm",
  "score": 0.9821,
  "latency_ms": 34.2,
  "model_version": "content-classifier-roberta-20260101T120000Z",
  "inference_at": "2026-01-01T12:00:00+00:00"
}
```

### Batch classification (up to 64 texts)
```bash
curl -s -X POST http://localhost:8000/classify/batch \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello world", "I will hurt you", "nice day"]}' | jq .
```
```json
{
  "results": [
    {"label": "safe", "score": 0.0321},
    {"label": "harm", "score": 0.9910},
    {"label": "safe", "score": 0.0150}
  ],
  "latency_ms": 41.7,
  "batch_size": 3,
  "model_version": "...",
  "inference_at": "..."
}
```

### Prometheus metrics
```bash
curl -s http://localhost:8000/metrics/ | grep classifier
# classifier_requests_total{endpoint="classify",label="harm"} 5.0
# classifier_request_latency_seconds_bucket{...}
```

### Interactive API docs (Swagger UI)
Open `http://localhost:8000/docs` in your browser.

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
```

### List bucket contents from your machine
```bash
# Install mcli if you don't have it (avoids conflict with GNU mc)
curl -sL https://dl.min.io/client/mc/release/linux-amd64/mc -o ~/.local/bin/mcli
chmod +x ~/.local/bin/mcli

mcli alias set local http://localhost:9000 sentinel sentinel-minio
mcli ls local/models/
mcli ls local/models/<run-id>/
```

### Object layout per optimizer run
```
models/
  <run-id>/
    fp32/                     — exported ONNX + tokenizer files
    o2/                       — O2 graph-optimized ONNX + tokenizer files
    int8/                     — INT8 quantized ONNX + tokenizer files (loaded by classifier)
    report.json               — full pipeline report (stage durations, minio paths, timestamps)
```

The classifier downloads the `int8/` directory from MinIO on startup and caches it locally at `/tmp/sentinel-model-cache/<run-id>/int8/`. Subsequent restarts with the same model version skip the download.

---

## Prometheus

**URL:** `http://localhost:9090`

Prometheus is deployed in the `sentinel-monitoring` namespace and scrapes the classifier at `host.k3d.internal:8000` every 10 seconds. `host.k3d.internal` is a hostname k3d injects into pod `/etc/hosts` that resolves to the Docker host — this allows Prometheus (running inside k3d) to reach the classifier running locally on your machine.

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090 &
```

### Useful queries
```
# Request rate by label (harm vs safe)
rate(classifier_requests_total[5m])

# P95 latency
histogram_quantile(0.95, rate(classifier_request_latency_seconds_bucket[5m]))

# Batch size distribution
rate(classifier_batch_size_bucket[5m])
```

### Check scrape targets
Open `http://localhost:9090/targets` — the `classifier` job should show `State: UP`.

---

## Grafana

**URL:** `http://localhost:3000` — login: `admin` / `admin`

Grafana is deployed in the `sentinel-monitoring` namespace. The Prometheus datasource is auto-provisioned via ConfigMap on first boot — no manual setup needed.

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000 &
```

### Getting started
1. Open `http://localhost:3000` and log in with `admin` / `admin`
2. Go to **Explore** → select the **Prometheus** datasource
3. Run a query like `classifier_requests_total` to verify data is flowing

Note: the browser-side datasource health check shows a cosmetic "Failed to fetch" warning because the browser cannot reach `http://prometheus:9090` (in-cluster DNS). Actual queries go through the Grafana backend proxy and work correctly.

---

## PostgreSQL

**Address:** `localhost:5432`
**Connection string:** `postgresql://sentinel:sentinel@localhost:5432/sentinel`

### Manual port-forward
```bash
kubectl port-forward -n sentinel-data svc/postgresql 5432:5432 &
```

### Connect with psql
```bash
psql postgresql://sentinel:sentinel@localhost:5432/sentinel
```

### Key tables

#### `model_registry`
Tracks every model version. The classifier reads this on startup to find the active model.

```sql
SELECT model_version, model_path, threshold, status, created_at, promoted_at
FROM model_registry
ORDER BY created_at DESC;
```

| Column | Type | Notes |
|---|---|---|
| `model_version` | VARCHAR UNIQUE | e.g. `content-classifier-roberta-20260101T120000Z` |
| `model_path` | TEXT | MinIO key: `models/<run-id>/int8/model_quantized.onnx` or absolute local path if MinIO was unavailable |
| `threshold` | FLOAT | Default 0.5 — score above this → `harm` |
| `status` | VARCHAR | `staging` → `active` → `retired` |
| `created_at` | TIMESTAMPTZ | |
| `promoted_at` | TIMESTAMPTZ | Set when Airflow promotes to active (Phase 7) |

Status flow: optimizer writes `staging` → Airflow evaluation promotes to `active` → previous version moves to `retired`.

The classifier prefers `active` models. If no active model exists (e.g. local dev before Airflow is wired up), it falls back to the most recent `staging` entry.

To manually promote a model for testing:
```sql
-- Retire the current active model
UPDATE model_registry SET status = 'retired' WHERE status = 'active';

-- Promote a staging model
UPDATE model_registry
SET status = 'active', promoted_at = NOW()
WHERE model_version = '<version>';
```

#### `classifications`
Every inference result. Written asynchronously by the classifier after each `/classify` call.

```sql
-- Recent classifications
SELECT ts, label, score, model_version, latency_ms
FROM classifications
ORDER BY ts DESC
LIMIT 20;

-- Label distribution
SELECT label, COUNT(*) AS n, ROUND(AVG(score)::numeric, 4) AS avg_score
FROM classifications
GROUP BY label;

-- P50 / P95 latency
SELECT
  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM classifications
WHERE ts > NOW() - INTERVAL '1 hour';
```

---

## MongoDB

**Address:** `localhost:27017`
**Connection URI:** `mongodb://sentinel:sentinel@localhost:27017/sentinel`

### Manual port-forward
```bash
kubectl port-forward -n sentinel-data svc/mongodb 27017:27017 &
```

### Connect with mongosh
```bash
mongosh "mongodb://sentinel:sentinel@localhost:27017/sentinel"
```

### Key collections

#### `flagged_content`
Harmful classifications stored as training samples for the retrain pipeline. The stream processor (Phase 5) writes here — not yet wired.

```js
// Recent flagged items
db.flagged_content.find().sort({ ts: -1 }).limit(10)

// Count by model version
db.flagged_content.aggregate([
  { $group: { _id: "$model_version", count: { $sum: 1 } } }
])
```

Document shape (when stream processor is wired in Phase 5):
```json
{
  "ts": "ISODate(...)",
  "input_text": "...",
  "label": "harm",
  "score": 0.98,
  "model_version": "...",
  "session_id": "...",
  "span_id": "..."
}
```

---

## mongo-express

**URL:** `http://localhost:8081` (no login required — local dev only)

Browser UI for inspecting MongoDB collections. Useful for verifying what the stream processor writes to `flagged_content`.

### Manual port-forward
```bash
kubectl port-forward -n sentinel-data svc/mongo-express 8081:8081 &
```

`dev-start.sh` opens this automatically. Navigate to `http://localhost:8081` → select the `sentinel` database → open `flagged_content`.

---

## Optimizer Pipeline

Runs locally. Downloads a HuggingFace model, exports to ONNX, applies O2 + INT8 quantization, uploads all three stages to MinIO, uploads the report to MinIO, and registers in `model_registry` as `staging`.

### Prerequisites
- MinIO port-forward on 9000 (or set `MINIO_ENDPOINT`)
- PostgreSQL port-forward on 5432 (or set `DATABASE_URL`)

### Run
```bash
uv run --package sentinel-optimizer python -m pipelines.optimizer.pipeline \
  --model-id VijayRam1812/content-classifier-roberta \
  --output-dir artifacts
```

### Environment variable overrides
```bash
MINIO_ENDPOINT=http://localhost:9000   # default
MINIO_ACCESS_KEY=sentinel              # default
MINIO_SECRET_KEY=sentinel-minio        # default
MINIO_BUCKET=models                    # default
DATABASE_URL=postgresql://sentinel:sentinel@localhost:5432/sentinel  # default
```

### What it produces
```
Local (gitignored):
  artifacts/<run-id>/fp32/     — FP32 ONNX + tokenizer
  artifacts/<run-id>/o2/       — O2 optimized ONNX + tokenizer
  artifacts/<run-id>/int8/     — INT8 quantized ONNX + tokenizer
  logs/optimizer/<run-id>/report.json

MinIO (primary, survives pod termination):
  models/<run-id>/fp32/        — FP32 artifacts
  models/<run-id>/o2/          — O2 artifacts
  models/<run-id>/int8/        — INT8 artifacts
  models/<run-id>/report.json  — full pipeline report

PostgreSQL:
  model_registry row: status = staging
```

If MinIO is unavailable, the local path is registered in model_registry instead. The classifier handles both formats transparently.

---

## Kubernetes — Inspect the Cluster

```bash
# All pods across sentinel namespaces
kubectl get pods -n sentinel-data
kubectl get pods -n sentinel-monitoring

# Services in data layer
kubectl get svc -n sentinel-data

# Check logs
kubectl logs -n sentinel-data statefulset/postgresql
kubectl logs -n sentinel-data statefulset/mongodb
kubectl logs -n sentinel-data statefulset/minio
kubectl logs -n sentinel-monitoring statefulset/prometheus
kubectl logs -n sentinel-monitoring deployment/grafana

# Check MinIO bucket init job result
kubectl get job minio-bucket-init -n sentinel-data
kubectl logs -n sentinel-data job/minio-bucket-init
```

---

## Port-Forward Reference (manual)

If you need to open individual tunnels without the startup script:

```bash
# PostgreSQL
kubectl port-forward -n sentinel-data svc/postgresql 5432:5432 &

# MongoDB
kubectl port-forward -n sentinel-data svc/mongodb 27017:27017 &

# MinIO (both API and console)
kubectl port-forward -n sentinel-data svc/minio 9000:9000 9001:9001 &

# Prometheus
kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090 &

# Grafana
kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000 &
```

---

---

## OTel Collector

**gRPC:** `localhost:4317`  **HTTP:** `http://localhost:4318`

Receives OTLP traces from any chat app or simulator, fans them out to:
- Kafka topic `traces.raw` (3 partitions, encoding: `otlp_json`)
- Jaeger (for trace visualisation)

### Send a test trace
```bash
python scripts/simulate-traces.py --count 10 --harm-pct 0.3
```

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/otel-collector 4317:4317 4318:4318 &
```

---

## Jaeger

**URL:** `http://localhost:16686`

In-memory trace store (local dev only — traces do not survive pod restarts). Search by service name `chat-app-simulator` after running `simulate-traces.py`.

### Manual port-forward
```bash
kubectl port-forward -n sentinel-monitoring svc/jaeger 16686:16686 &
```

---

## Stream Processor

Runs locally (started by `dev-start.sh`). Consumes `traces.raw`, calls `/classify/batch` on the classifier, writes results to PostgreSQL and MongoDB.

**Logs:** `tail -f /tmp/sentinel-pf/stream-processor.log`

### Data flow
```
Kafka traces.raw
  → extract LLM spans (prompt + response)
  → POST /classify/batch (persist=False)
  → write to PostgreSQL classifications  (all)
  → write to MongoDB flagged_content     (harm + 10% safe sample)
  → commit Kafka offset
```

### Verify it's working
```bash
# After running simulate-traces.py:
psql postgresql://sentinel:sentinel@localhost:5432/sentinel \
  -c "SELECT label, count(*) FROM classifications GROUP BY label;"

# Check MongoDB
mongosh "mongodb://sentinel:sentinel@localhost:27017/sentinel" \
  --eval "db.flagged_content.find().sort({ts:-1}).limit(5)"
```

### Key design decisions
- **Manual offset commit** after successful PG + MongoDB writes — Kafka redelivers on failure
- `persist=False` on `/classify/batch` prevents the classifier from double-writing to PG
- `ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING` deduplicates replays
- `SAFE_SAMPLE_RATE=0.1` (10%) prevents class imbalance in the MongoDB retraining dataset
- `traces.raw` has 3 partitions — scaling stream processor beyond 3 replicas gives no benefit

### classifications table (Phase 5 additions)
Two new columns added by the Phase 5 schema migration (run on every `dev-start.sh`):

| Column | Type | Notes |
|---|---|---|
| `span_id` | TEXT | OTLP span ID — nullable (classifier's own writes have no span_id) |
| `text_type` | VARCHAR(8) | `"prompt"` or `"response"` |

---

## What Is Not Yet Deployed (Upcoming Phases)

| Component | Phase | What it enables |
|---|---|---|
| Spark | 6 | Batch drift detection (PSI/JSD) |
| Airflow | 7 | Orchestrates retrain + promote when PSI > 0.2 |
| MLflow | 7 | Experiment tracking for retrain runs |
