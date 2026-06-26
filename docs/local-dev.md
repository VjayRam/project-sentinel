# Sentinel — Local Dev Reference

Everything you need to reach, inspect, and test every part of the stack.

---

## Quick Start

```bash
./scripts/dev-start.sh
```

Starts k3d, applies Terraform, opens port-forwards, and launches the classifier.
Press **Ctrl-C** to stop everything cleanly.

If you have a specific model to load:
```bash
MODEL_PATH=/path/to/int8 ./scripts/dev-start.sh
```

---

## Service Map

| Service | Local URL / Address | Credentials | Status |
|---|---|---|---|
| Classifier API | `http://localhost:8000` | — | Phase 1 ✓ |
| Classifier docs | `http://localhost:8000/docs` | — | Phase 1 ✓ |
| Prometheus scrape | `http://localhost:8000/metrics` | — | Phase 1 ✓ |
| MinIO console | `http://localhost:9001` | `sentinel` / `sentinel-minio` | Phase 3 ✓ |
| MinIO S3 API | `http://localhost:9000` | access: `sentinel` secret: `sentinel-minio` | Phase 3 ✓ |
| PostgreSQL | `localhost:5432` | `sentinel` / `sentinel` / db `sentinel` | Phase 3 ✓ |
| MongoDB | `localhost:27017` | `sentinel` / `sentinel` / db `sentinel` | Phase 3 ✓ |
| Prometheus | `http://localhost:9090` | — | Phase 4 (not deployed) |
| Grafana | `http://localhost:3000` | `admin` / `admin` | Phase 4 (not deployed) |

---

## Classifier

**Base URL:** `http://localhost:8000`

### Health check
```bash
curl http://localhost:8000/health
# {"status":"ok","model":"martin-ha/toxic-comment-model"}
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
  "model_version": "toxic-comment-model-20250101T120000Z",
  "inference_at": "2025-01-01T12:00:00+00:00"
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
curl -s http://localhost:8000/metrics | grep classifier
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
```

### Object key format for model artifacts
```
models/
  <run-id>/
    model_quantized.onnx    ← loaded by the classifier
    config.json
    tokenizer_config.json
    tokenizer.json
    vocab.txt
    special_tokens_map.json
```

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
| `model_version` | VARCHAR UNIQUE | e.g. `toxic-comment-model-20250101T120000Z` |
| `model_path` | TEXT | MinIO key: `models/<run-id>/model_quantized.onnx` |
| `threshold` | FLOAT | Default 0.5 — score above this → `harm` |
| `status` | VARCHAR | `staging` → `active` → `retired` |
| `created_at` | TIMESTAMPTZ | |
| `promoted_at` | TIMESTAMPTZ | Set when Airflow promotes to active |

Status flow: optimizer writes `staging` → Airflow evaluation promotes to `active` → previous version moves to `retired`.

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
  "ts": ISODate("..."),
  "input_text": "...",
  "label": "harm",
  "score": 0.98,
  "model_version": "...",
  "session_id": "...",
  "span_id": "..."
}
```

---

## Optimizer Pipeline

Runs locally. Downloads a HuggingFace model, exports to ONNX, applies O2 + INT8 quantization, uploads to MinIO, and registers in `model_registry` as `staging`.

### Prerequisites
- MinIO port-forward on 9000 (or set `MINIO_ENDPOINT`)
- PostgreSQL port-forward on 5432 (or set `DATABASE_URL`)

### Run
```bash
uv run python -m pipelines.optimizer.pipeline \
  --model-id martin-ha/toxic-comment-model \
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
- Local artifacts at `artifacts/<run-id>/fp32/`, `o2/`, `int8/`
- Run report at `logs/optimizer/<run-id>/report.json`
- MinIO: `models/<run-id>/model_quantized.onnx` + config files
- PostgreSQL: row in `model_registry` with `status = staging`

---

## Kubernetes — Inspect the Cluster

```bash
# All pods across sentinel namespaces
kubectl get pods -A -l managed-by=terraform 2>/dev/null; \
kubectl get pods -n sentinel-data; \
kubectl get pods -n sentinel-app; \
kubectl get pods -n sentinel-monitoring

# Data layer services
kubectl get svc -n sentinel-data

# Check PostgreSQL logs
kubectl logs -n sentinel-data statefulset/postgresql

# Check MongoDB logs
kubectl logs -n sentinel-data statefulset/mongodb

# Check MinIO logs
kubectl logs -n sentinel-data statefulset/minio

# Check init job result
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

# Prometheus (Phase 4 — not yet deployed)
# kubectl port-forward -n sentinel-monitoring svc/prometheus 9090:9090 &

# Grafana (Phase 4 — not yet deployed)
# kubectl port-forward -n sentinel-monitoring svc/grafana 3000:3000 &
```

---

## What Is Not Yet Deployed (Upcoming Phases)

| Component | Phase | What it enables |
|---|---|---|
| Prometheus | 4 | Scrapes `/metrics` from classifier; stores time-series |
| Grafana | 4 | Dashboards for `classifications_total`, `latency_seconds`, `model_version_info` |
| OTel Collector | 4 | Receives OTLP traces from the chat app on `:4317` |
| Jaeger | 4 | Trace visualisation |
| Kafka | 5 | Message bus: OTel traces → stream processor |
| Stream processor | 5 | Consumes traces, calls `/classify`, writes to PostgreSQL + MongoDB |
| Spark | 6 | Batch drift detection (PSI/JSD) |
| Airflow | 7 | Orchestrates retrain + promote when PSI > 0.2 |
| MLflow | 7 | Experiment tracking for retrain runs |
