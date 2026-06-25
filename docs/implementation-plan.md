# Sentinel — Structured Learning Implementation Plan

The goal is not just to build Sentinel but to deeply learn each tool through deliberate,
progressive implementation. Each phase teaches the WHY before the HOW, introduces tools
in dependency order, includes a deliberate experiment to break or stress the tool (the
fastest way to understand it), and ends with a concrete checkpoint that proves both that
the system works and that you understand it.

## Current State

| Component | Status |
|---|---|
| Model optimization pipeline (`optimize.py`) | ✅ Done |
| Classifier service (`main.py`) | ✅ Done |
| CI/CD (GitHub Actions → GHCR) | ✅ Done |
| DB schema (`001_initial_schema.sql`) | ✅ Done |
| Terraform scaffold (providers only) | ✅ Done |
| Everything below | ❌ Not started |

---

## Phase 1 — Terraform + Storage Layer (Week 1)

### What you will learn
What "state" means in infrastructure-as-code. Why the gap between `terraform plan`
and `terraform apply` matters. How modules enable the same code to serve local and
cloud environments. What happens when real infrastructure drifts from declared state.

### What to build

Five Terraform modules, wired together with explicit `depends_on`:

```
terraform/modules/
  kubernetes/    → 4 namespace resources (imports the manually-created ones)
  databases/     → postgresql + mongodb via bitnami Helm charts
  storage/       → minio Helm chart + bucket init Job
  monitoring/    → kube-prometheus-stack + jaeger Helm charts + OTel Collector Deployment
  ml_serving/    → classifier Deployment + Service + HPA + ServiceMonitor
```

Each module takes `namespace` and `resource_limits` variables so the same HCL
applies to cloud environments with different values passed from `prod.tfvars`.

**DB init:** a Kubernetes Job inside the `databases` module runs
`db/postgres/migrations/001_initial_schema.sql` after the Helm release is ready.
The Job's initContainer polls `pg_isready` before executing — no hardcoded sleep.

**Model loading:** the `ml_serving` module's classifier Deployment has an
initContainer that pulls `models/onnx_quantized/model.onnx` from MinIO at pod
startup using the `mc` CLI. This replaces the hostPath approach and works
identically in cloud when MinIO is swapped for S3.

### Deliberate experiment — state drift
After `terraform apply` succeeds:
1. Manually delete the `sentinel-data` namespace: `kubectl delete namespace sentinel-data`
2. Run `terraform plan` — observe Terraform detects the drift and plans recreation
3. Run `terraform apply` — watch it recreate everything in dependency order

This teaches you concretely what "desired state reconciliation" means. The plan output
tells you exactly what Terraform will do before it does anything.

### Deliberate experiment — import existing resources
The 4 namespaces were created manually in Phase 0 with `kubectl`. Instead of deleting
and recreating them:
1. `terraform import kubernetes_namespace.sentinel_app sentinel-app`
2. Repeat for the other 3 namespaces
3. Run `terraform plan` — it should show no changes for those resources

This teaches you the difference between resources Terraform created vs resources
Terraform adopted. Critical knowledge for working with existing infrastructure.

### Checkpoint
```bash
kubectl get pods -n sentinel-data        # postgresql, mongodb, minio → Running
kubectl get pods -n sentinel-monitoring  # prometheus, grafana, jaeger → Running
kubectl get pods -n sentinel-app         # classifier ×2 → Running

# DB tables exist
kubectl exec -n sentinel-data postgresql-0 -- psql -U sentinel -c '\dt'
# Should show: classifications, drift_stats, model_registry

# Classifier loaded model from model_registry (or env fallback)
kubectl logs -n sentinel-app deployment/classifier | grep "model"

# MinIO buckets exist
kubectl port-forward -n sentinel-data svc/minio 9001:9001
# open http://localhost:9001 → models, datasets, mlflow-artifacts buckets
```

### Interview talking point
> "I used Terraform with the Kubernetes and Helm providers to provision 11 services
> across 4 namespaces. I deliberately tested state drift — manually deleting a namespace
> and watching terraform plan detect and plan the recreation. I also used terraform import
> to take ownership of namespaces that were created manually."

---

## Phase 2 — Observability: OTel + Jaeger + Prometheus + Grafana (Week 2)

### What you will learn
The three pillars of observability (traces, metrics, logs) and how they connect to each
other. The OTel Collector's pipeline model (receiver → processor → exporter). The four
Prometheus metric types (counter, gauge, histogram, summary). How to write PromQL queries
from scratch rather than copying them.

### What to build

**OTel Collector ConfigMap** (`monitoring/otel/config.yaml`):
- OTLP gRPC receiver on `:4317`
- Batch processor (5s timeout, 256 batch size)
- `spanmetrics` connector → Prometheus exporter on `:8889` (auto-generates
  request rate + latency histograms from span data, without any code changes)
- Jaeger exporter on `:14250`
- Kafka exporter on `traces.raw` topic (commented out — enabled in Phase 3)

**4 Grafana dashboards** (`monitoring/grafana/dashboards/`):

| Dashboard | Key panels |
|---|---|
| `classifier.json` | Inference latency (p50/p95/p99), classification rate by label, confidence distribution, active model version |
| `pipeline.json` | OTel spans/sec, Kafka consumer lag (Phase 3 onwards), stream processor throughput |
| `drift.json` | PSI over time, JSD over time, confidence decay trend, threshold breach markers |
| `model-versions.json` | model_registry timeline, accuracy/F1/AUC per version, latency comparison across versions |

**Prometheus alerting rules** (`monitoring/prometheus/rules.yaml`):

| Alert | Condition |
|---|---|
| `ClassifierHighLatency` | p95 > 100ms for 5 min |
| `OtelCollectorDropping` | `otelcol_receiver_refused_spans > 0` |
| `DriftThresholdBreached` | `sentinel_drift_psi > 0.2` |
| `ClassifierDown` | no `sentinel_classifications_total` increase for 10 min |

### Deliberate experiment — trace a request end to end
Create `tests/integration/trace_test.py` — a script that sends 10 fake OTLP spans
with `llm.request.prompt` set to a test string (no real chat app needed). Then:
1. Watch the span appear in Jaeger
2. Watch `llm_request_duration_milliseconds` appear in Prometheus (via spanmetrics)
3. Watch it populate the Grafana pipeline dashboard

This teaches you exactly how data flows through: producer → collector → Kafka/Jaeger/Prometheus → visualization.
You will understand every hop in the pipeline.

### Deliberate experiment — trigger and resolve an alert
1. Scale the classifier to 0: `kubectl scale deployment/classifier -n sentinel-app --replicas=0`
2. Wait 10 minutes — watch `ClassifierDown` fire in AlertManager
3. Scale back to 2 — watch the alert resolve

This teaches you alert lifecycle states (inactive → pending → firing → resolved) and
the difference between a firing alert and a resolved one.

### PromQL to write yourself (not copy-paste)
```promql
# Classification rate per second by label
rate(sentinel_classifications_total[5m])

# p95 inference latency
histogram_quantile(0.95, rate(sentinel_classification_latency_seconds_bucket[5m]))

# Harmful fraction of all classifications
rate(sentinel_classifications_total{result="harmful"}[5m])
/ rate(sentinel_classifications_total[5m])
```

Writing these from scratch — understanding why `rate()` is needed for counters,
why `histogram_quantile` needs the `_bucket` suffix, what `[5m]` means — is the
difference between "I set up Prometheus" and "I understand Prometheus" in an interview.

### Checkpoint
```bash
# Send a test OTLP span
python tests/integration/trace_test.py

# Jaeger: see the span
kubectl port-forward -n sentinel-monitoring svc/jaeger-query 16686:16686
# http://localhost:16686

# Prometheus: see sentinel_classifications_total
kubectl port-forward -n sentinel-monitoring svc/prometheus-operated 9090:9090
# http://localhost:9090

# Grafana: all 4 dashboards have data
kubectl port-forward -n sentinel-monitoring svc/grafana 3000:80
# http://localhost:3000
```

### Interview talking point
> "I built 4 Grafana dashboards and Prometheus alerting rules from scratch. I understand
> the OTel Collector's pipeline model — the spanmetrics connector converts trace spans
> into Prometheus histograms automatically, giving me latency and throughput metrics
> without changing the classifier service. I can write PromQL queries using rate(),
> histogram_quantile(), and label filtering."

---

## Phase 3 — Kafka + Python Stream Processor (Week 3)

### What you will learn
Kafka's core primitives: topics, partitions, consumer groups, and offset management.
The difference between at-least-once and exactly-once delivery. Why partition count
is the maximum concurrency ceiling for a consumer group.

### What to build

**Kafka via Strimzi** (new Terraform module `terraform/modules/kafka/`):
- KafkaCluster CRD: 1 broker for local
- Topics: `traces.raw` (3 partitions), `classification`, `drift.alerts`, `retrain.events`

**Stream Processor** (`services/stream-processor/main.py`):
```
Consumer loop (consumer group: sentinel-stream-processor):
  poll()
  → deserialize span JSON
  → extract llm.request.prompt + llm.response.content from attributes
  → POST /classify to classifier service (HTTP)
  → INSERT into postgresql classifications (idempotent: ON CONFLICT DO NOTHING)
  → if label == 'harmful': INSERT into mongodb flagged_content
  → publish classification result to Kafka classification topic
  → commit offset manually (AFTER successful DB write — not before)
```

**Key design decision:** manual offset commit (not auto-commit). If the PostgreSQL write
fails, the offset is not committed. On consumer restart, the message is reprocessed.
The `ON CONFLICT DO NOTHING` on `classifications` prevents duplicate rows.
This is the at-least-once + idempotent writes pattern.

### Deliberate experiment — consumer group partition assignment
1. Start with 1 stream-processor replica — it owns all 3 partitions
2. Scale to 2 replicas: `kubectl scale deployment/stream-processor --replicas=2`
3. Watch logs — you will see a `PartitionAssignmentChanged` rebalance event
4. Scale to 4 replicas — one gets 0 partitions. Kafka has 3 partitions, so only 3
   consumers can be active at once regardless of how many you run

This teaches you exactly why "number of partitions = maximum parallelism" and
why you can't just scale consumers infinitely.

### Deliberate experiment — consumer lag under load
1. Scale stream-processor to 0 (pause consumer)
2. Send 500 test spans to `traces.raw`
3. Check lag: `kafka-consumer-groups.sh --describe --group sentinel-stream-processor`
   (lag = 500 — all messages are queued, none processed)
4. Scale back to 2 — watch lag drain in real time in Grafana pipeline dashboard

This is the most concrete demonstration of why Kafka's persistence model is valuable:
the messages didn't disappear while the consumer was down.

### Deliberate experiment — at-least-once delivery in practice
1. Inject a random fault: wrap MongoDB write in try/except that raises 10% of the time
2. Observe that the Kafka offset is NOT committed for failed messages
3. Restart the consumer — those messages are reprocessed
4. Verify no duplicate rows in PostgreSQL (`classifications` count vs messages processed)

### Checkpoint
```bash
# Send 100 test spans
python tests/integration/send_spans.py --count 100

# Consumer lag should drain to 0
kubectl exec -n sentinel-data kafka-0 -- \
  kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --describe --group sentinel-stream-processor

# DB writes completed
kubectl exec -n sentinel-data postgresql-0 -- \
  psql -U sentinel -c 'SELECT COUNT(*), label FROM classifications GROUP BY label'

# MongoDB has harmful content only
kubectl exec -n sentinel-data mongodb-0 -- \
  mongosh sentinel --eval 'db.flagged_content.countDocuments()'
```

### Interview talking point
> "I configured a 3-partition Kafka topic with a Python consumer group of 2 replicas.
> I used manual offset commits so that a failed DB write causes the message to be
> reprocessed rather than silently dropped. I experimentally verified that scaling to
> 4 consumer replicas gave no benefit beyond 3 — because partition count is the
> concurrency ceiling."

---

## Phase 4 — Spark Drift Detection (Week 4)

### What you will learn
Spark's lazy evaluation model — why nothing executes until an action is called.
The difference between narrow and wide transformations (and why wide ones cause shuffles).
How window functions enable time-series aggregations. How spark-on-k8s-operator
manages driver and executor pods.

### What to build

**Spark batch job** (`ml/drift/spark_drift_job.py`):
```python
# Read classifications from PostgreSQL via JDBC
df = spark.read.jdbc(url=DB_URL, table="classifications", ...)

# Reference period: first 7 days of production data
reference = df.filter(col("ts") < reference_cutoff)

# Sliding window aggregation
window = Window.orderBy("ts").rangeBetween(-3600, 0)  # 1-hour windows

# Compute:
#   PSI  — compare label distribution in window vs reference
#   JSD  — Jensen-Shannon divergence on confidence score distribution
#   confidence_decay — rolling mean of confidence scores

# Write to drift_stats table
results.write.jdbc(url=DB_URL, table="drift_stats", mode="append")

# Publish to Kafka if threshold breached
if psi > 0.2 or jsd > 0.1:
    producer.send("drift.alerts", value=alert_payload)
```

**SparkApplication CRD** (`k8s/base/spark-drift-job.yaml`):
```yaml
driver: 1 CPU, 1Gi
executors: 2 instances × 1 CPU, 2Gi each
image: custom (pyspark + psycopg2 + kafka-python)
```

Submit manually in Phase 4; Airflow submits it in Phase 5.

### Deliberate experiment — lazy evaluation + `.explain()`
1. Add `.explain(extended=True)` before `.write()` in the Spark job
2. Run the job — look at the physical plan in the executor logs
3. Count the number of exchanges (shuffles)
4. Add `.cache()` on the reference DataFrame and re-run
5. Compare the physical plan — one fewer full scan of the reference data
6. Compare runtime — the cached run should be measurably faster

This teaches you why Spark's lazy evaluation exists (it can optimize the full plan
before executing) and when caching eliminates redundant computation.

### Deliberate experiment — inject drift and detect it
1. Run the data simulator: `python services/data-simulator/main.py --harmful-rate 0.9 --count 500`
   (normal baseline is ~30% harmful; inject 90%)
2. Wait for stream processor to classify and write to `classifications`
3. Run the Spark job manually: `kubectl apply -f k8s/base/spark-drift-job.yaml`
4. Check `drift_stats` — PSI should exceed 0.2
5. Check `drift.alerts` Kafka topic — should have a breach message
6. Check Grafana drift dashboard — should show the PSI spike

### Checkpoint
```bash
# Apply the SparkApplication
kubectl apply -f k8s/base/spark-drift-job.yaml
kubectl get sparkapplication drift-detector -w
# Wait for: Status: COMPLETED

# Drift stats were written
kubectl exec -n sentinel-data postgresql-0 -- \
  psql -U sentinel -c 'SELECT metric, value, threshold, breached FROM drift_stats ORDER BY ts DESC LIMIT 5'

# Drift alert was published (if breach)
kubectl exec -n sentinel-data kafka-0 -- \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic drift.alerts --from-beginning --max-messages 5
```

### Interview talking point
> "I implemented PSI and JSD drift detection as a Spark batch job on spark-on-k8s-operator.
> I used `.explain()` to inspect the physical execution plan and found a redundant full scan
> of the reference dataset. Caching it with `.cache()` reduced job runtime by 40%. I also
> learned that wide transformations (aggregations, joins) cause shuffles which are the main
> performance bottleneck in Spark."

---

## Phase 5 — Airflow + MLflow (Week 5)

### What you will learn
DAG-based orchestration: why pipelines need idempotency, what XComs are and when to
use them vs a shared database, and the difference between `poke` and `reschedule` sensor
modes. MLflow's experiment tracking model and when to use it vs a custom table.

### What to build

**Three DAGs** (`airflow/dags/`):

`drift_monitor_dag.py` (schedule: `*/15 * * * *`):
```
SparkKubernetesOperator: submit drift-detector SparkApplication
→ PostgresSensor (reschedule mode): wait for drift_stats row with breached=True
→ TriggerDagRunOperator: trigger retrain_dag if sensor fires
```

`retrain_dag.py` (triggered by drift_monitor_dag or manually):
```
PythonOperator: run optimize.py against updated training data from MongoDB
→ PythonOperator: evaluate new model on hold-out set, log metrics to MLflow run
→ BranchPythonOperator: compare new accuracy vs current active model in model_registry
  ├─ if better:  UPDATE model_registry SET status='active' WHERE version=new
  │              kubectl rollout restart deployment/classifier
  └─ if worse:   log regression to MLflow, skip deploy, send alert
```

`data_pipeline_dag.py` (schedule: `@daily`):
```
PythonOperator: ETL flagged_content from MongoDB
  → deduplicate → balance classes → write to training_dataset table
→ PythonOperator: archive to MinIO datasets bucket
```

**MLflow** (new Terraform module or added to `ml_serving`):
- Backend store: PostgreSQL (`mlflow` database, separate from `sentinel`)
- Artifact store: MinIO `mlflow-artifacts` bucket
- Deploy via `community/mlflow` Helm chart

### Deliberate experiment — task-level retry
1. Intentionally make the MLflow logging step fail once (raise if run already exists)
2. In Airflow UI: click the failed task → Clear → re-run just that task
3. Observe: upstream tasks do not re-run, downstream tasks resume from this task
4. Compare to what would happen if this were a script calling functions in sequence

This teaches you the fundamental value of DAG-based orchestration: task-level
granularity for retries and re-runs.

### Deliberate experiment — sensor mode comparison
1. Set PostgresSensor with `mode='poke'` — watch it hold a worker slot while polling
2. Switch to `mode='reschedule'` — watch it release the slot between polls
3. Check Airflow's worker utilization — `reschedule` frees slots for other tasks

This teaches you why `reschedule` is the production-correct choice for sensors with
long wait times.

### Deliberate experiment — compare runs in MLflow UI
1. Run `retrain_dag` twice with different data sizes (500 vs 2000 training samples)
2. Open MLflow UI: compare the two runs side by side
3. Observe accuracy/F1/AUC as a function of training set size
4. Register the better model to MLflow's own Model Registry as `challenger`
5. Note: MLflow's registry is for comparison; Sentinel's `model_registry` table is for
   deployment. Keep them in sync manually — document this as a known gap.

### Checkpoint
```bash
# Full end-to-end: inject drift → detect → retrain → deploy

# 1. Inject drift
python services/data-simulator/main.py --harmful-rate 0.9 --count 500

# 2. Trigger drift_monitor_dag (or wait for 15-min schedule)
# Airflow UI: http://localhost:8080 → drift_monitor_dag → Trigger DAG

# 3. Watch retrain_dag run (all tasks green in Graph view)

# 4. Verify new model promoted
kubectl exec -n sentinel-data postgresql-0 -- \
  psql -U sentinel -c "SELECT version, status, accuracy FROM model_registry ORDER BY trained_at DESC LIMIT 3"

# 5. Verify classifier rolling restart picked up new model
kubectl rollout status deployment/classifier -n sentinel-app
kubectl logs -n sentinel-app deployment/classifier | grep "Loaded active model"

# 6. MLflow UI: two runs visible, metrics logged
kubectl port-forward -n sentinel-monitoring svc/mlflow 5000:5000
# http://localhost:5000
```

### Interview talking point
> "I built three Airflow DAGs for drift monitoring, model retraining, and data ETL.
> I used a PostgresSensor in reschedule mode to avoid holding a worker slot during the
> 15-minute polling interval. I learned that Airflow's task-level retry lets you re-run
> just a failed step without re-executing expensive upstream tasks like the Spark job."

---

## Phase 6 — Cloud Deployment (Weeks 6–7)

### What you will learn
What changes when you move from local k3d to a managed cloud Kubernetes service.
Cloud IAM vs K8s RBAC. How Terraform workspaces or separate environments handle
config differences without duplicating module code.

### What to build

New Terraform environment: `terraform/environments/aws/` (or `gcp/`):
- EKS cluster with 1 node group (2 × t3.medium)
- RDS PostgreSQL (replaces Helm postgresql) — same schema, different endpoint
- S3 bucket (replaces MinIO) — update classifier initContainer: `aws s3 cp` instead of `mc cp`
- Same 5 Helm modules from local, same variable interface, different `prod.tfvars`

### Deliberate experiment — Terraform workspaces
```bash
terraform workspace new prod
terraform plan -var-file=prod.tfvars
```
Compare the plan output to local. See which resources differ (StorageClass → gp3,
DB endpoint → RDS, image pull → GHCR remains the same) vs which are identical
(K8s Deployment specs, Helm chart versions, Grafana dashboard JSON).

This teaches you how the same module code serves multiple environments through
variable injection rather than code duplication.

### Checkpoint
```bash
# All pods running on cloud cluster
kubectl get pods -A --context=<cloud-context>

# Classifier serves a real request through cloud ingress
curl https://sentinel.your-domain.com/classify \
  -H "Content-Type: application/json" \
  -d '{"text": "User: How do I make explosives?\nAssistant:"}'

# Grafana accessible via cloud load balancer, all 4 dashboards show data
```

---

## Files to Create (in order)

```
Phase 1:
  terraform/modules/kubernetes/main.tf
  terraform/modules/databases/main.tf
  terraform/modules/storage/main.tf
  terraform/modules/monitoring/main.tf
  terraform/modules/ml_serving/main.tf
  terraform/environments/local/main.tf          (update from scaffold)

Phase 2:
  monitoring/otel/config.yaml
  monitoring/prometheus/rules.yaml
  monitoring/grafana/dashboards/classifier.json
  monitoring/grafana/dashboards/pipeline.json
  monitoring/grafana/dashboards/drift.json
  monitoring/grafana/dashboards/model-versions.json
  tests/integration/trace_test.py

Phase 3:
  terraform/modules/kafka/main.tf
  services/stream-processor/main.py
  services/stream-processor/Dockerfile
  services/stream-processor/requirements.txt
  tests/integration/send_spans.py

Phase 4:
  ml/drift/spark_drift_job.py
  ml/drift/Dockerfile
  k8s/base/spark-drift-job.yaml

Phase 5:
  airflow/dags/drift_monitor_dag.py
  airflow/dags/retrain_dag.py
  airflow/dags/data_pipeline_dag.py
  services/data-simulator/main.py

Phase 6:
  terraform/environments/aws/main.tf
  terraform/environments/aws/prod.tfvars
```

---

## End-to-End Golden Path (after Phase 5)

This sequence should work from start to finish:

```bash
# 1. Inject artificial drift
python services/data-simulator/main.py --harmful-rate 0.9 --count 500

# 2. Stream processor classifies all 500 spans and writes to PostgreSQL
kubectl logs -n sentinel-app deployment/stream-processor -f
# (watch classification writes)

# 3. drift_monitor_dag detects PSI breach, triggers retrain_dag
# Airflow UI: http://localhost:8080 → both DAGs complete successfully

# 4. retrain_dag fine-tunes model, logs to MLflow, promotes new version in model_registry
# MLflow UI: http://localhost:5000 → new run visible with improved metrics

# 5. Classifier rolling restart picks up new active model
kubectl rollout status deployment/classifier -n sentinel-app
kubectl logs -n sentinel-app deployment/classifier | grep "Loaded active model from registry"

# 6. Grafana drift dashboard shows: PSI spike → retrain → recovery
# http://localhost:3000 → Drift dashboard
```

---

## Weekly Schedule Summary

| Week | Phase | Primary tools | Key concept to understand |
|---|---|---|---|
| 1 | Terraform + storage | Terraform, Helm, K8s PVCs | Infrastructure state and drift |
| 2 | Observability | OTel Collector, Jaeger, Prometheus, Grafana | Traces vs metrics vs logs |
| 3 | Kafka + stream processor | Kafka, confluent-kafka-python | Consumer groups and offset management |
| 4 | Spark drift detection | PySpark, spark-on-k8s-operator | Lazy evaluation and window functions |
| 5 | Airflow + MLflow | Airflow DAGs, MLflow | Orchestration vs scripting |
| 6–7 | Cloud deployment | EKS/GKE, Terraform workspaces | Local → cloud with no module changes |
