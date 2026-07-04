# Terraform (local) — Explanation

This workspace deploys the entire Sentinel infrastructure into a local k3d
Kubernetes cluster with a single `terraform apply`. Every K8s resource — from
namespaces and Secrets to StatefulSets, Jobs, and ConfigMaps — is declared here.
No `kubectl apply` files exist; Terraform is the single source of truth.

---

## Why Terraform over kubectl / Helm

- **State tracking**: Terraform records what it deployed in `terraform.tfstate`.
  On the next apply it diffs state against reality and only touches what changed.
- **Dependency graph**: `depends_on` and implicit resource references let you
  express "Kafka must exist before the topic init Job runs" cleanly.
- **Reviewable diffs**: `terraform plan` shows exactly what will change before
  any mutation happens.
- **Idempotent**: `terraform apply` is safe to run repeatedly — it converges to
  the declared state without destroying things that haven't changed.

---

## providers.tf

```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.27" }
    helm       = { source = "hashicorp/helm",       version = "~> 2.12" }
  }
}

provider "kubernetes" {
  config_path    = "~/.kube/config"
  config_context = var.k8s_context
}
```

**`required_version`** — pins the minimum Terraform CLI version. The `~> 2.27`
constraint on the kubernetes provider means "2.27.x or any 2.x above it, but
not 3.x". This is the recommended operator for providers: accept patches but not
major breaking changes.

**`config_context = var.k8s_context`** (default: `k3d-sentinel`) — k3d writes a
new context into `~/.kube/config` when it creates a cluster. The provider reads
from that context. If you run multiple clusters (e.g., `k3d-sentinel` and
`minikube`), this variable lets you target the right one without editing the
provider block.

**The Helm provider is present** but currently unused (Helm charts were
considered for PostgreSQL/MongoDB but replaced with native Kubernetes resources
to avoid Bitnami's authentication changes). It's kept because Phase 7 may use
Helm for Airflow.

**Tip:** run `kubectl config get-contexts` to see all available contexts. The
Terraform var must match exactly.

---

## variables.tf

All tuneable values are declared here. The defaults are safe for local dev —
no production credentials are hardcoded.

| Variable | Default | Purpose |
|---|---|---|
| `k8s_context` | `k3d-sentinel` | kubectl context name |
| `postgres_password` | `sentinel` | sentinel user password |
| `postgres_storage_size` | `2Gi` | PVC size for PostgreSQL data |
| `mongodb_root_password` | `sentinel-root` | MongoDB root user password |
| `mongodb_password` | `sentinel` | MongoDB sentinel user password |
| `mongodb_storage_size` | `2Gi` | PVC size for MongoDB data |
| `minio_root_user` | `sentinel` | MinIO root (S3 access key) |
| `minio_root_password` | `sentinel-minio` | MinIO root (S3 secret key) |
| `minio_storage_size` | `5Gi` | PVC size for MinIO data |
| `prometheus_storage_size` | `5Gi` | PVC size for Prometheus TSDB |
| `grafana_admin_password` | `admin` | Grafana admin login |
| `kafka_storage_size` | `2Gi` | PVC size for Kafka topic data |
| `airflow_admin_password` | `sentinel` | Airflow webserver admin login |
| `airflow_webserver_secret_key` | (dev-only static string) | Flask session-signing key — see the Airflow section's gotcha #7 for why this must not be left unset |

**To override without editing the file**, use `-var`:
```bash
terraform apply -var="postgres_storage_size=10Gi" -var="kafka_storage_size=5Gi"
```

Or create a `terraform.tfvars` file (gitignored):
```hcl
postgres_password   = "mysecretpassword"
grafana_admin_password = "betterpassword"
```

**Sensitive variables** are marked `sensitive = true`. Terraform redacts their
values from `plan` and `apply` output. They still appear in `terraform.tfstate`
in plaintext — never commit the state file when it contains real secrets.

---

## main.tf — Resource by resource

### Namespaces

```hcl
locals {
  namespaces = ["sentinel-app", "sentinel-data", "sentinel-monitoring", "sentinel-pipeline"]
}

resource "kubernetes_namespace" "sentinel" {
  for_each = toset(local.namespaces)
  metadata {
    name   = each.key
    labels = { "managed-by" = "terraform" }
  }
}
```

`for_each = toset(local.namespaces)` creates one `kubernetes_namespace` resource
per string in the list. Terraform treats this as a map keyed by the string value.
Reference a specific namespace elsewhere: `kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name`.

**Why four namespaces?** Namespace isolation serves two purposes:
1. **RBAC scope**: in production, the stream processor only needs access to
   `sentinel-data` services, not `sentinel-pipeline` or `sentinel-monitoring`.
2. **Network policies**: you can restrict which pods can talk to which services
   at the namespace level using Kubernetes NetworkPolicy resources.

**The `managed-by = terraform` label** lets you quickly find all Terraform-managed
namespaces: `kubectl get ns -l managed-by=terraform`.

---

### Pattern: Secret → ConfigMap → StatefulSet → Service

All stateful services (PostgreSQL, MongoDB, MinIO, Kafka) follow this four-resource
chain. Each step `depends_on` the previous:

1. **Secret** — stores passwords, keys, and credentials. Injected into pods via
   `secretKeyRef`. Never stored in env vars or ConfigMaps in plaintext.
2. **ConfigMap** — stores init scripts, config files, or provisioning data.
   Mounted as files into the pod.
3. **StatefulSet** — the workload. Uses `volumeClaimTemplate` to request a
   PersistentVolumeClaim per replica. The PVC uses `local-path` StorageClass
   (k3d's built-in, backed by a directory on the host).
4. **Service** — exposes the StatefulSet's pods. ClusterIP by default — not
   accessible from outside the cluster without a port-forward.

---

### PostgreSQL

```hcl
resource "kubernetes_secret" "postgres" {
  data = { password = var.postgres_password }
}
```

The password is only stored in the K8s Secret and the Terraform state. It is
injected into the PostgreSQL pod via `POSTGRES_PASSWORD` env var and read by
the classifier via `DATABASE_URL` (assembled in dev-start.sh after syncing the
secret's value with `kubectl get secret ... | base64 -d`).

**Why `dev-start.sh` reads the password from the Secret rather than using
the variable directly?**

The PostgreSQL init script only runs on first boot (empty PVC). If you delete
and recreate the Secret with a different password (e.g., after a `terraform
apply` with a new `postgres_password` var), the running PostgreSQL still has
the old password baked into its data directory. `dev-start.sh` runs
`ALTER USER sentinel PASSWORD '...'` via the local socket (which bypasses
password auth) to sync the password every time.

**Init SQL (in ConfigMap `postgresql-init`)**

The ConfigMap key `01_schema.sql` is mounted at
`/docker-entrypoint-initdb.d/01_schema.sql`. The official `postgres:16` image
executes all `*.sql` files in that directory in lexicographic order on first
boot. Filenames prefixed with `01_`, `02_` etc. control execution order.

The schema creates:
- `model_registry` — tracks every ONNX model version with a status FSM
  (`staging → active → retired`) enforced by a CHECK constraint.
- `classifications` — every inference result, with `span_id`/`text_type`
  columns added in Phase 5 for stream processor idempotency.
- A partial unique index on `(span_id, text_type) WHERE span_id IS NOT NULL`
  — enables `ON CONFLICT ... DO NOTHING` for Kafka redelivery scenarios.
- Three indexes on `classifications` for common query patterns:
  `(ts DESC)` for recency queries, `(label, ts DESC)` for label-filtered
  time queries, `(model_version, ts DESC)` for per-version analysis.

**`TIMESTAMPTZ` everywhere, never `TIMESTAMP`** — `TIMESTAMP` stores a naive
datetime with no timezone info. `TIMESTAMPTZ` stores UTC and converts on
display. In a system where pods, databases, and application code may run in
different timezones, `TIMESTAMP` causes silent bugs in date arithmetic.

**FK constraint `classifications.model_version → model_registry.model_version`**:
You cannot INSERT a classification row unless the model_version already exists
in model_registry. You cannot DELETE a model_registry row that has associated
classifications — use `UPDATE SET status = 'retired'` instead.

```hcl
wait_for_rollout = true
timeouts { create = "3m" }
```

`wait_for_rollout = true` causes Terraform to block until the StatefulSet's
rollout is complete (all replicas ready). Without this, downstream resources
that depend on the pod (like the MinIO bucket init Job) would attempt to
connect before the pod is ready.

---

### MongoDB

Same pattern as PostgreSQL. Two secrets: root password (for admin operations)
and sentinel user password (for application access).

```hcl
env { name = "MONGO_INITDB_ROOT_USERNAME"; value = "root" }
env { name = "MONGO_INITDB_ROOT_PASSWORD"; value_from { secret_key_ref { key = "root-password" } } }
```

The init JS is mounted at `/docker-entrypoint-initdb.d/init.js`. It:
1. Switches to the `sentinel` database.
2. Creates the `sentinel` user with `readWrite` role.
3. Creates the `flagged_content` collection with a schema validator.
4. Creates a TTL index on `ts` (configurable expiry for old training data).
5. Creates an index on `(label, ts)` for label-filtered queries.

**Why MongoDB for flagged content?**

The `flagged_content` collection stores variable-shape documents — the span
metadata fields (session_id, trace_id, llm_model) are optional and may expand
as the OTel schema evolves. A document store handles schema evolution without
migrations. PostgreSQL would require an ALTER TABLE for every new attribute.

---

### MinIO

```hcl
env { name = "MINIO_ROOT_USER";     value = var.minio_root_user }
env { name = "MINIO_ROOT_PASSWORD"; value = var.minio_root_password }
```

MinIO speaks the S3 API. The classifier uses boto3 to download model artifacts.
The optimizer uses boto3 to upload them. No actual AWS account is involved —
`MINIO_ENDPOINT=http://localhost:9000` overrides boto3's default AWS endpoint.

**Bucket init Job:**

```hcl
resource "kubernetes_job_v1" "minio_init" {
  spec {
    template {
      spec {
        container {
          command = ["/bin/sh", "-c", <<-SCRIPT
            until mc alias set minio http://minio:9000 ...; do sleep 3; done
            mc mb --ignore-existing minio/models
            mc mb --ignore-existing minio/datasets
          SCRIPT]
        }
      }
    }
    backoff_limit = 4
  }
  wait_for_completion = true
  depends_on = [kubernetes_stateful_set.minio, kubernetes_service.minio]
}
```

The `until` loop retries the `mc alias set` command until MinIO is ready.
This handles the race condition where the Job pod starts before MinIO has
finished initializing. `backoff_limit = 4` means Kubernetes restarts the Job
pod up to 4 times if it fails — combined with the retry loop, this tolerates
slow MinIO startup.

`--ignore-existing` means the Job is idempotent — running it again (on
`terraform apply`) doesn't fail if the buckets already exist.

**Object layout after the optimizer runs:**
```
models/
  <run-id>/
    fp32/    — FP32 ONNX + tokenizer
    o2/      — O2 graph-optimized
    int8/    — INT8 quantized (what the classifier loads)
    report.json
```

---

### Prometheus

```hcl
resource "kubernetes_config_map" "prometheus_config" {
  data = {
    "prometheus.yml" = file("${path.module}/../../../prometheus/prometheus.yml")
    "classifier-rules.yml" = file("${path.module}/../../../prometheus/rules/classifier.yml")
  }
}
```

`file(...)` reads the content of the local file at plan time and embeds it into
the ConfigMap. `path.module` is the directory of the current `.tf` file
(`infra/terraform/local/`), so `../../../prometheus/` resolves to
`infra/prometheus/`. This keeps the Prometheus config in `infra/prometheus/`
(editable, version-controlled) while Terraform pushes it into the cluster.

**Volume mount with `sub_path`:**

```hcl
volume_mount {
  name       = "config"
  mount_path = "/etc/prometheus/prometheus.yml"
  sub_path   = "prometheus.yml"
}
```

Mounting a ConfigMap without `sub_path` replaces the entire directory. Using
`sub_path` mounts only the named key as a specific file, leaving the rest of
`/etc/prometheus/` intact. This is required when the container expects other
files in that directory (like the rules subdirectory).

**`host.k3d.internal` scraping:**

Prometheus runs inside k3d pods but scrapes the classifier running on the host.
k3d automatically injects `host.k3d.internal → <Docker bridge IP>` into the
`/etc/hosts` of all its pods, enabling this cross-boundary scrape. This is
analogous to `host.docker.internal` in plain Docker Desktop.

**`--web.enable-lifecycle`** — enables `POST /-/reload` to hot-reload config
without restarting the pod. After editing prometheus.yml or rules, apply the
ConfigMap change with Terraform and reload:
```bash
kubectl rollout restart statefulset/prometheus -n sentinel-monitoring
# or, if you just need rule changes:
kubectl exec -n sentinel-monitoring statefulset/prometheus -- \
  curl -s -X POST http://localhost:9090/-/reload
```

---

### Grafana

```hcl
resource "kubernetes_config_map" "grafana_provisioning" {
  data = {
    "datasources.yml" = file("${path.module}/../../../grafana/provisioning/datasources/prometheus.yml")
  }
}
```

Same `file(...)` pattern as Prometheus. The datasource YAML is embedded in the
ConfigMap and mounted at `/etc/grafana/provisioning/datasources/`.

**Deployment, not StatefulSet** — Grafana is stateless from Prometheus's
perspective (it doesn't store metrics). Its own PVC (`grafana_data`) holds the
SQLite database for dashboards and user settings created via the UI. A Deployment
is correct here: no stable network identity is needed, and the pod can be freely
rescheduled without data loss because the PVC follows the pod via the claim.

---

### mongo-express

```hcl
env {
  name  = "ME_CONFIG_MONGODB_URL"
  value = "mongodb://root:$(ME_CONFIG_MONGODB_ROOT_PASSWORD)@mongodb:27017/?authSource=admin"
}
```

**K8s env var substitution** (`$(VAR_NAME)`) lets you compose env values from
other env vars in the same pod spec. This avoids duplicating the root password —
one env var reads it from the Secret, the second builds the full URL from it.

**Why root credentials?** mongo-express calls `db.adminCommand({ serverStatus: 1
})` on startup to verify connectivity. This admin command requires the `root`
user (with `authSource=admin`). The application sentinel user has only
`readWrite` on the `sentinel` database and cannot run admin commands.

**`ME_CONFIG_BASICAUTH=false`** — disables mongo-express's own basic auth login.
Acceptable for local dev on a private network; never disable in production.

---

### Kafka

Uses the **official `apache/kafka` image**, not Bitnami — so env vars are the
bare `KAFKA_*` names (`KAFKA_PROCESS_ROLES`, `KAFKA_LISTENERS`, etc.), *not*
Bitnami's `KAFKA_CFG_*`-prefixed convention. Easy to mix up if you've worked
with the Bitnami chart before; the two images silently ignore each other's env
var names instead of erroring, so a `KAFKA_CFG_X` var on this image just does
nothing.

```hcl
env { name = "KAFKA_PROCESS_ROLES";    value = "broker,controller" }
env { name = "KAFKA_CONTROLLER_QUORUM_VOTERS"; value = "1@localhost:9093" }
```

**KRaft mode** (no ZooKeeper) — Kafka 3.3+ includes KRaft as production-stable.
One process handles both the broker role (serving producers/consumers) and the
controller role (managing cluster metadata, topic assignments, leader elections).
For a single-node local setup, `localhost:9093` works for the controller quorum
voter because the controller and broker are in the same pod.

**Two listeners:**

```hcl
env { name = "KAFKA_LISTENERS";
      value = "PLAINTEXT://:9092,CONTROLLER://:9093,EXTERNAL://:9094" }
env { name = "KAFKA_ADVERTISED_LISTENERS";
      value = "PLAINTEXT://kafka.sentinel-data.svc.cluster.local:9092,EXTERNAL://localhost:9094" }
```

- `PLAINTEXT:9092` — in-cluster listener. The OTel Collector (in sentinel-monitoring)
  connects to `kafka.sentinel-data.svc.cluster.local:9092`. This is how
  cross-namespace in-cluster traffic reaches Kafka.
- `CONTROLLER:9093` — internal only, used for KRaft consensus. Never exposed
  outside the pod.
- `EXTERNAL:9094` — the host-facing listener. Its advertised address is
  `localhost:9094`. When the stream processor connects via `kubectl port-forward
  ... 9094:9094`, Kafka returns `localhost:9094` as the broker address in its
  metadata response. Subsequent connections from the stream processor also go
  through the port-forward — it all stays consistent.

**`ADVERTISED_LISTENERS` is the key to understanding Kafka networking.** It's
what Kafka tells clients to use after the initial metadata fetch. If this is
wrong, clients can connect initially but fail when they try to produce/consume.

**`KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1`** — the `__consumer_offsets`
internal topic defaults to a replication factor of 3. With only one broker, 3
replicas are impossible and Kafka would never create the topic, leaving consumers
unable to commit offsets. Setting it to 1 makes single-broker operation possible.

**Topic init Job:**

```hcl
command = ["/bin/sh", "-c",
  "kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists
   --topic traces.raw --partitions 3 --replication-factor 1 && echo 'topic ready'"
]
```

`--if-not-exists` makes the Job idempotent. `kafka-topics.sh` exits 0 whether
or not it created the topic. Without this flag, a second `terraform apply`
would see the Job "fail" (exit 1 because the topic already exists).

**Why 3 partitions?** Each Kafka partition can be consumed by exactly one
consumer in a consumer group at a time. With `GROUP_ID = "sentinel-stream-processor"`,
scaling to 3 stream processor replicas gives full parallelism — each replica
owns one partition. A 4th replica would sit idle. 3 is the right choice for a
service with at most 3 meaningful replicas.

**Tip:** inspect the topic after startup:
```bash
kubectl exec -n sentinel-data statefulset/kafka -- \
  kafka-topics.sh --bootstrap-server localhost:9092 --describe --topic traces.raw
```

---

### Gotcha: PVC mounted, but Kafka wasn't writing to it

The `data` volume has always been mounted at `/bitnami/kafka` (a path name
left over from when Bitnami's image was being evaluated), but for a long time
nothing told Kafka to actually put its KRaft log segments there. The official
`apache/kafka` image's default `log.dirs` is `/tmp/kraft-combined-logs` — a
path that has nothing to do with the mounted PVC. Every pod restart was
silently wiping all topic data, because the "persistent" volume was never
where Kafka actually wrote.

**Fix:** set `KAFKA_LOG_DIRS` explicitly to a path under the mount:

```hcl
env {
  name  = "KAFKA_LOG_DIRS"
  value = "/bitnami/kafka/data"
}
```

**How this was caught:** not by reading the image's docs — by restarting the
`kafka-0` pod live and comparing `kafka-consumer-groups.sh --describe` output
(topic offsets, consumer group lag) before and after. Identical output after
a restart is the actual proof persistence works; a green `kubectl get pods`
proves nothing about whether the *data* survived.

### Gotcha: pinning the image version can be a silent downgrade

Following the general "never `:latest`, always pin" rule, `apache/kafka:latest`
got pinned to `apache/kafka:3.9.0` — a specific, well-tested version. This
immediately crash-looped every broker start with:

```
java.lang.IllegalArgumentException: No MetadataVersion with feature level 30
```

Root cause: `:latest` had already drifted to Kafka 4.3.1 by the time this PVC
was first formatted (confirmed by inspecting the cached image's jar filename:
`kafka_2.13-4.3.1.jar`), and KRaft's on-disk metadata format is
**forward-compatible only** — an older broker cannot read metadata a newer
one wrote. `3.9.0` is *older* than whatever had been running, so pinning to it
was a downgrade, not a stability improvement.

**The fix isn't "always pin to the newest stable release"** — it's "pin to
whatever version is actually compatible with data already on disk, or wipe
the PVC first." For a fresh cluster with no existing data, pinning to 3.9.0
would have been fine. For this one, the right pin was 4.3.1 (matching what
had already formatted the volume). **If you ever bump this version going
forward, wipe the PVC first** (`kubectl delete pvc data-kafka-0 -n sentinel-data`)
unless you've confirmed the new version can read the old metadata format.

---

### Jaeger

```hcl
env { name = "COLLECTOR_OTLP_ENABLED"; value = "true" }
```

Jaeger all-in-one bundles the collector, query service, and UI into a single
container. It stores traces in memory — traces do not survive pod restarts. For
production, use a persistent storage backend (Cassandra, Elasticsearch, or
Jaeger's native OTEL backend with S3).

`COLLECTOR_OTLP_ENABLED=true` activates the OTLP gRPC receiver on port 4317.
Without this, Jaeger only accepts Jaeger-format traces via the UDP Thrift agent
on port 6831 (the older protocol). The OTel Collector sends OTLP, so this flag
is required.

**Tip:** search traces by `service.name` = `chat-app-simulator` after running
`scripts/simulate-traces.py`. Each span shows the LLM prompt/response attributes.

---

### OTel Collector

```hcl
resource "kubernetes_config_map" "otel_collector_config" {
  data = {
    "config.yaml" = <<-YAML
    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317 }
          http: { endpoint: 0.0.0.0:4318 }
    processors:
      batch:
        timeout: 1s
        send_batch_size: 100
    exporters:
      kafka:
        brokers: [kafka.sentinel-data.svc.cluster.local:9092]
        topic: traces.raw
        encoding: otlp_json
      otlp/jaeger:
        endpoint: jaeger.sentinel-monitoring.svc.cluster.local:4317
        tls: { insecure: true }
    extensions:
      health_check: { endpoint: 0.0.0.0:13133 }
    service:
      extensions: [health_check]
      pipelines:
        traces:
          receivers: [otlp]
          processors: [batch]
          exporters: [kafka, otlp/jaeger]
    YAML
  }
}
```

**Pipeline model**: the collector is built around a
`receivers → processors → exporters` pipeline. Data flows in one direction.
Each stage is independently configurable and composable.

**`batch` processor:**
- `timeout: 1s` — flush the batch after 1 second even if `send_batch_size` isn't
  reached. This caps end-to-end latency to ~1s at low traffic.
- `send_batch_size: 100` — flush when 100 spans accumulate, regardless of time.
  At higher traffic, batching reduces Kafka produce calls.

The two parameters together give: at most 100 spans per message, and at most 1
second of buffering. In practice, most batches will flush on the timeout at low
traffic and on the size limit at high traffic.

**`encoding: otlp_json`** — spans are serialized as `ExportTraceServiceRequest`
JSON and written to Kafka. The stream processor reads this format and parses it
with `processor.extract_spans()`. The alternative `otlp_proto` (binary protobuf)
is more compact but harder to inspect manually:
```bash
# Inspect a raw Kafka message
kubectl exec -n sentinel-data statefulset/kafka -- \
  kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic traces.raw --max-messages 1
```

**`otlp/jaeger` exporter** — the `/jaeger` suffix is an OTel Collector naming
convention for multiple instances of the same exporter type. You can have
`otlp/jaeger` and `otlp/tempo` both configured, exporting to different backends.

**`health_check` extension** — exposes an HTTP endpoint at `:13133` that returns
200 when the collector is ready. Kubernetes's `readiness_probe` uses this to
gate traffic until the collector has connected to all its downstream exporters
(Kafka and Jaeger). Without this, the readiness probe would have to use a TCP
socket check which only verifies the port is open, not that the exporters are
connected.

**`depends_on` ordering:**
```hcl
depends_on = [
  kubernetes_job_v1.kafka_topic_init,
  kubernetes_deployment.jaeger,
  kubernetes_service.jaeger,
]
```

The OTel Collector starts after:
1. The Kafka topic `traces.raw` is created (init Job completes).
2. Jaeger is ready (Deployment + Service exist and are healthy).

If the Collector starts before `traces.raw` exists, it fails to connect to
Kafka and enters a CrashLoopBackOff. The `depends_on` prevents this.

**Tip — add a debug exporter during development:**
```yaml
exporters:
  debug:
    verbosity: detailed   # logs every span to stdout
  kafka: ...
  otlp/jaeger: ...

service:
  pipelines:
    traces:
      exporters: [kafka, otlp/jaeger, debug]
```

Then `kubectl logs -n sentinel-monitoring deployment/otel-collector -f` shows
every span as it arrives. Remove `debug` before committing.

---

### spark-operator (sentinel-pipeline)

```hcl
resource "helm_release" "spark_operator" {
  repository = "https://kubeflow.github.io/spark-operator"
  chart      = "spark-operator"
  version    = "~2.1"
  namespace  = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  values = [yamlencode({
    controller = { workers = 1 }
    webhook    = { enable = true }
    spark      = { jobNamespaces = ["sentinel-pipeline"] }
  })]
}
```

The operator itself doesn't run Spark jobs — it watches for `SparkApplication`
custom resources (defined in `pipelines/drift/spark-application.yaml`) and
translates each one into a driver pod + N executor pods, using its own
`spark` ServiceAccount to create/delete them. `spark.jobNamespaces` scopes
which namespaces it's allowed to watch — restricting it to `sentinel-pipeline`
means a `SparkApplication` submitted anywhere else is simply ignored, not an
error.

**RBAC**: the driver pod runs as its own `kubernetes_service_account.spark`,
bound to a `kubernetes_role.spark_driver` with `create/get/list/watch/delete/
deletecollection/update/patch` on `pods/services/configmaps/
persistentvolumeclaims`. The driver needs to create and later clean up its
own executor pods — `deletecollection` specifically is required for the
operator's cleanup step; without it, completed/failed SparkApplications leave
orphaned executor pods behind (`Forbidden` errors in the operator's logs are
the tell).

See [`../../pipelines/drift/explanation.md`](../../pipelines/drift/explanation.md)
for what actually runs inside the driver/executor pods.

---

### Airflow (sentinel-pipeline, `airflow.tf`)

Deployed via the official Apache Airflow Helm chart with `LocalExecutor` —
tasks run as subprocesses of the scheduler pod, so there's no Celery/Redis/
Flower to operate at this scale. Reuses the existing PostgreSQL instance (a
separate `airflow` database) instead of the chart's bundled Postgres subchart.

```hcl
resource "helm_release" "airflow" {
  chart   = "airflow"
  version = "1.15.0"   # exact version, not "~> 1.15" — see gotcha below
  values  = [yamlencode({
    executor   = "LocalExecutor"
    redis      = { enabled = false }
    flower     = { enabled = false }
    statsd     = { enabled = false }
    triggerer  = { enabled = false }
    postgresql = { enabled = false }
    migrateDatabaseJob = { enabled = true, useHelmHooks = false }
    data = { metadataConnection = { host = "postgresql.sentinel-data.svc.cluster.local", db = "airflow", ... } }
    scheduler = { extraVolumes = [...], extraVolumeMounts = local.dag_volume_mounts }
    webserver = { extraVolumes = [...], extraVolumeMounts = local.dag_volume_mounts }
  })]
}
```

DAGs are mounted from a `kubernetes_config_map` built by reading every file
in `orchestration/*.py` (same `file()`-and-embed pattern used for Prometheus
config and the Postgres init scripts above) — no git-sync sidecar, no custom
image to rebuild every time a DAG changes.

This deployment surfaced more live-only gotchas than anything else in this
repo. In the order they were actually hit:

**1. `airflow` database has to exist before the chart's migration Job can
run.** The Postgres init SQL that creates it (`CREATE DATABASE airflow OWNER
sentinel;`, in `main.tf`'s `postgres_init` ConfigMap) only executes on a
*fresh* Postgres data directory — it does nothing on a cluster whose Postgres
already has data. On an existing cluster, create it manually once:
```bash
echo "CREATE DATABASE airflow OWNER sentinel;" | \
  kubectl exec -i -n sentinel-data postgresql-0 -- psql -U sentinel -d sentinel
```

**2. `migrateDatabaseJob.enabled` needs to be explicit.** Without it, the
scheduler/webserver's `wait-for-airflow-migrations` init container
crash-loops forever — no migration Job ever gets created to satisfy it. The
chart's default didn't reliably create the Job when using an external
(non-subchart) Postgres. Set it explicitly:
```hcl
migrateDatabaseJob = { enabled = true, useHelmHooks = false }
```

**3. `extraVolumes`/`extraVolumeMounts` are per-component, not top-level.**
Setting them at the top level of the values object is accepted by YAML/Helm
with *no error at all* — it's just silently ignored, because this chart
scopes those keys under `scheduler.*`, `webserver.*`, `workers.*`, etc., not
globally. The tell: `/opt/airflow/dags` exists but is empty on every pod, no
matter how correct the ConfigMap itself looks. Nest under the specific
component:
```hcl
scheduler = { extraVolumes = [...], extraVolumeMounts = [...] }
webserver = { extraVolumes = [...], extraVolumeMounts = [...] }
```

**4. A ConfigMap mounted as a directory breaks Airflow's DAG file walker.**
Even correctly mounted, `airflow dags list` failed with:
```
RuntimeError: Detected recursive loop when walking DAG directory /opt/airflow/dags:
/opt/airflow/dags/..2026_07_03_05_03_34.641811799 has appeared more than once.
```
Kubernetes mounts ConfigMap volumes through a `..data -> ..<timestamp>`
symlink indirection so updates are atomic. Airflow's DAG-directory walker
(`find_path_from_directory`) doesn't understand that structure and aborts.
**Fix:** mount each DAG file individually via `subPath` instead of mounting
the ConfigMap as a whole directory — `subPath` mounts bypass the symlink
indirection entirely and appear as plain files:
```hcl
locals {
  dag_volume_mounts = [
    for f in fileset("${path.module}/../../../orchestration", "*.py") : {
      name = "dags", mountPath = "/opt/airflow/dags/${f}", subPath = f, readOnly = true
    }
  ]
}
```
One `volumeMount` per DAG file — more entries as `orchestration/` grows, but
generated dynamically from the same `fileset()` the ConfigMap's `data` uses,
so it never needs manual updates.

**5. `"~> 1.15"` version constraints can break `helm_release` imports.** Using
a loose constraint produced `Error: Provider produced inconsistent final
plan... was cty.StringVal("~> 1.15"), but now cty.StringVal("1.15.0")` — a
known Helm-provider quirk where the constraint resolves to a concrete version
mid-apply. Pin the exact version instead; it also matches the "never
`:latest`" spirit anyway.

**6. An interrupted `terraform apply` can leave a healthy release stuck in
`pending-install`.** If the apply is killed after Helm's `helm install` has
already started server-side, the underlying pods can come up completely
healthy while Helm's own bookkeeping (a `sh.helm.release.v1.<name>.v1`
Secret) never gets marked `deployed`. Symptom: `helm list` shows
`pending-install` and any new `helm upgrade`/`terraform apply` fails with
`cannot re-use a name that is still in use`. **Don't reflexively
uninstall+reinstall** — that destroys working pods for no reason. Instead,
patch the release Secret's stored status directly:
```bash
# decode .data.release (double base64 + gzip), fix info.status to "deployed",
# re-encode, kubectl patch the secret, then:
terraform import helm_release.airflow "sentinel-pipeline/airflow"
```

**7. `webserverSecretKeySecretName` needs to be set explicitly, or you get a
warning banner and unstable sessions.** Without either `webserverSecretKey`
or `webserverSecretKeySecretName`, the chart auto-generates a random Flask
secret key **on every deploy** — every `terraform apply` that touches the
release invalidates all webserver sessions, and Airflow shows a persistent
"Usage of a dynamic webserver secret key detected" dashboard warning. Fix:
create your own Secret and point the chart at it:
```hcl
resource "kubernetes_secret" "airflow_webserver_secret_key" {
  metadata { name = "airflow-webserver-secret-key-static" }  # NOT the chart's
                                                                # own default name
                                                                # (already taken
                                                                # by its auto-
                                                                # generated one)
  data = { webserver-secret-key = var.airflow_webserver_secret_key }
}
# then: webserverSecretKeySecretName = kubernetes_secret.airflow_webserver_secret_key.metadata[0].name
```
Verify it's actually static by deleting the webserver pod and diffing the
Secret's value before/after — it should be byte-for-byte identical.

**8. Port 8080 is not actually free in a k3d cluster.** The Airflow webserver
listens on 8080 inside the pod, so the obvious port-forward is
`8080:8080` — but k3d's own `<cluster>-serverlb` container already publishes
host port 8080 for its own ingress (`0.0.0.0:8080->80/tcp`), invisible to
`lsof`/`fuser` because it's a Docker-managed listener, not a process in this
shell's namespace. `kubectl port-forward` silently loses that race. Forward
to a different **local** port instead — the remote/pod side stays 8080:
```bash
kubectl port-forward -n sentinel-pipeline svc/airflow-webserver 8090:8080
```

**Tip — the fastest way to check "did my DAG actually load":**
```bash
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags list-import-errors     # "No data found" == clean
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags trigger healthcheck && sleep 15 && \
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags list-runs -d healthcheck
```

**9. Editing a DAG file and re-applying Terraform updates the ConfigMap, but
not the mounted file — a direct consequence of gotcha #4's own fix.**
Mounting each DAG via `subPath` (to dodge the DAG-walker's recursive-loop
bug) means bypassing the `..data -> ..<timestamp>` symlink Kubernetes
normally uses to make ConfigMap updates appear live with no pod restart.
Trading away the symlink to fix the walker also trades away the live-update
behavior — confirmed live by editing `orchestration/retrain_dag.py`,
running `terraform apply`, and `grep`-ing the file's content inside the
scheduler pod to find it unchanged. **Any DAG file edit needs an explicit
restart of both pods that mount it:**
```bash
kubectl rollout restart statefulset/airflow-scheduler -n sentinel-pipeline
kubectl rollout restart deployment/airflow-webserver -n sentinel-pipeline
```

**10. The stable REST API needs its own auth backend enabled — the
webserver UI login working doesn't imply the API accepts the same
credentials.** `services/label-ui` triggers `retrain_dag` via `POST
/api/v1/dags/retrain_dag/dagRuns` using HTTP Basic auth with the same
admin/`<password>` the UI login form uses — but the REST API validates
requests against `[api] auth_backends`, a separate config surface from the
webserver's own session-based login. Without setting it explicitly, that
first API call failed even though the UI login worked fine. Fixed with an
explicit `config` block in the Helm values (matching this file's established
"don't trust chart defaults silently" pattern — see gotcha #2):
```hcl
config = {
  api = { auth_backends = "airflow.api.auth.backend.basic_auth" }
}
```

**11. `KubernetesPodOperator` needs its own RBAC — separate from the
rollout-restart Role.** `retrain_dag.py`'s `run_retraining` task launches a
pod (`sentinel-retraining:local`) to do the actual fine-tuning — this needs
permission to `create`/`get`/`list`/`watch`/`delete` `pods` (+ `get`/`list`
on `pods/log`) in `sentinel-pipeline`, which is a *different* permission
from `kubernetes_role.airflow_rollout`'s `apps/deployments` patch access in
`sentinel-app`. Added as a second Role/RoleBinding pair
(`airflow_pod_launcher`) bound to the same `airflow` ServiceAccount — same
shape as `spark_driver`'s Role, just for a different SA:
```hcl
resource "kubernetes_role" "airflow_pod_launcher" {
  rule { api_groups = [""]; resources = ["pods"];     verbs = ["create","get","list","watch","delete"] }
  rule { api_groups = [""]; resources = ["pods/log"]; verbs = ["get","list"] }
}
```

**12. A third RBAC surface: custom resources are their own API group,
separate from core-API pods.** `drift_dag.py` (Phase 7.4) submits/polls/
deletes a `SparkApplication` — a custom resource in the
`sparkoperator.k8s.io` group, not the core `""` group `airflow_pod_launcher`
above covers. Needed its own Role:
```hcl
resource "kubernetes_role" "airflow_spark_application" {
  rule {
    api_groups = ["sparkoperator.k8s.io"]
    resources  = ["sparkapplications", "sparkapplications/status"]
    verbs      = ["create", "get", "list", "watch", "delete"]
  }
}
```
This is unrelated to `spark_driver`'s Role (`main.tf`) — that one lets the
**driver pod** manage its own executor pods once spark-operator's
controller has already created it from the CR; this one lets the
**Airflow SA** create/watch/delete the CR in the first place.

**13. `scheduler.env` needs `DATABASE_URL` explicitly — the scheduler pod
has no way to reach the "sentinel" database otherwise.** `data.
metadataConnection` above configures Airflow's connection to *its own*
metadata database (named `airflow`) — a completely separate database on
the same Postgres instance from `model_registry`/`drift_stats`/
`classifications` (the `sentinel` database). Both `retrain_dag.py`'s
`decide_promotion` and `drift_dag.py`'s `check_drift` read
`os.environ["DATABASE_URL"]` directly to reach the latter. This was a
**latent bug for a while**: `decide_promotion` was written and deployed
without this env var ever being set, and it went unnoticed because the
first test run raised its own `ValueError` (quality gate failed) *before*
ever reaching the `psycopg2.connect(os.environ["DATABASE_URL"])` line —
the missing env var only became visible once a run actually needed to read
it. A reminder that a code path "working" in one test doesn't mean every
line in it executed. Fixed by adding it to the scheduler's `env`, reusing
the same `drift-postgres` secret already mirrored into this namespace for
the drift job's driver pod — one secret, three consumers (the driver pod,
`decide_promotion`, `check_drift`), no duplication.

**14. A Connection was added, then removed, once the approach it supported
was abandoned.** An earlier version of `drift_dag.py` used
`apache-airflow-providers-cncf-kubernetes`'s `SparkKubernetesOperator`/
`SparkKubernetesSensor`, which go through Airflow's own `KubernetesHook`
and need a real `Connection` object (not just "happens to be running
in-cluster") — added via `AIRFLOW_CONN_KUBERNETES_DEFAULT` as a JSON env
var (`jsonencode({conn_type = "kubernetes", extra = {in_cluster = true}})`
— JSON has worked directly in `AIRFLOW_CONN_*` since Airflow 2.3, sidestepping
the fragile provider-specific URI-extra-field encoding). That whole
approach was later abandoned (see
[`../../orchestration/explanation.md`](../../orchestration/explanation.md)'s
`drift_dag.py` section for why) in favor of plain
`kubernetes.config.load_incluster_config()` calls, which need no Airflow
Connection at all — so this env var was removed again once nothing
referenced it. Worth knowing if you ever see a stray `AIRFLOW_CONN_*` var
that looks orphaned: check whether the code that needed it is still there
before assuming it's still load-bearing.

---

### MLflow (sentinel-monitoring, `mlflow.tf`)

A `kubernetes_deployment`/`kubernetes_service` pair, not a Helm chart — no
official chart exists for MLflow the way one does for Airflow. Deployment
(not StatefulSet), same reasoning as Grafana: all real state lives
elsewhere (Postgres backend store, MinIO artifact store), so the pod itself
is stateless and freely reschedulable.

```hcl
args = [
  "server",
  "--backend-store-uri", "postgresql://sentinel:$(PG_PASSWORD)@postgresql.../mlflow",
  "--default-artifact-root", "s3://mlflow/",
  "--serve-artifacts",
  "--workers", "2",
  "--allowed-hosts", join(",", ["localhost", "localhost:5000", "mlflow", ...]),
]
```

**Custom image, not the official one directly** (`infra/mlflow/Dockerfile`,
own explanation.md) — `ghcr.io/mlflow/mlflow` doesn't bundle a Postgres
driver or S3 client, so `--backend-store-uri postgresql://...` and
`--default-artifact-root s3://...` both fail to start against the base
image.

**Reuses existing infra rather than standing up new stateful services** —
same philosophy as Airflow's separate `airflow` database on the existing
Postgres instance: a `03_mlflow_db.sql` init script adds an `mlflow`
database, and the MinIO bucket-init Job gets an `mlflow` bucket alongside
`models`/`datasets`.

**Three live-only bugs, all found by actually deploying it, not by reading
docs:**

1. **The `03_mlflow_db.sql` init script never ran** — same class of gotcha
   as Airflow's #1: Postgres init scripts only execute against a *fresh*
   data directory, and this cluster's Postgres already had data from
   earlier phases. Fixed the same way: create the database manually once
   against the live instance (`CREATE DATABASE mlflow OWNER sentinel;`);
   the SQL script stays in `main.tf` so a from-scratch cluster still gets
   it automatically.
2. **OOMKilled at both 512Mi and 1Gi memory limits.** MLflow's FastAPI/
   uvicorn tracking server defaults to 4 worker processes — heavier than
   the single-process Grafana/Jaeger images this resource block was
   originally copied from. Fixed with `--workers 2` and a `2Gi`/`768Mi`
   limit/request — the node had ~10Gi free the whole time, this was purely
   a cgroup limit being too tight, not real memory pressure.
3. **403 "Invalid Host header — possible DNS rebinding attack detected"**
   when the retraining pod (a different namespace) connected. MLflow 3.5+
   ships a security middleware that only allows `localhost` + private IPs
   by default — an in-cluster DNS name like
   `mlflow.sentinel-monitoring.svc.cluster.local` isn't recognized.
   `--allowed-hosts` **replaces** the default rather than extending it, so
   `localhost` had to be re-added explicitly too, or the port-forwarded
   UI/browser access would have broken instead. Matching is against the
   full `Host:port` header, not just the hostname — bare `mlflow` 403'd
   just like the DNS name did until the `:5000`-suffixed forms were added
   too.

---

### Label UI (sentinel-app, `label-ui.tf`)

Plain `kubernetes_deployment`/`kubernetes_service`, same shape as
classifier/stream-processor — no new pattern introduced. The only thing
worth noting here is credential mirroring: this service needs both the
`sentinel-app`-local `app_mongodb` secret (already mirrored there for other
app services) *and* `airflow_admin_password`, which otherwise only exists
in `sentinel-pipeline` — K8s Secrets are namespace-scoped, so a new
`app_airflow` secret mirrors that one value into `sentinel-app` too, same
pattern as `app_mongodb`/`app_minio`.

```hcl
resource "kubernetes_secret" "app_airflow" {
  metadata { name = "airflow-credentials"; namespace = "sentinel-app" }
  data     = { admin-password = var.airflow_admin_password }
}
```

See [`../../services/label-ui/explanation.md`](../../services/label-ui/explanation.md)
for what the service actually does with these credentials (triggering
`retrain_dag` via Airflow's REST API).

---

## outputs.tf

All outputs are port-forward commands. Running `terraform output` after `apply`
shows them:

```bash
terraform output kafka_port_forward
# kubectl port-forward -n sentinel-data svc/kafka 9094:9094
```

`dev-start.sh` opens all port-forwards automatically. The outputs are useful
when you want to open individual tunnels manually:
```bash
$(terraform output -raw jaeger_port_forward) &
$(terraform output -raw otel_collector_grpc_port_forward) &
```

**`airflow_webserver_port_forward` forwards local `8090` to the pod's `8080`**
— the only asymmetric one. `kubectl port-forward <local>:<remote>` supports
different port numbers on each side; only the local side needed to move here
(k3d's own load balancer already owns host port 8080), the Service and pod
both still listen on 8080 internally.

**`mlflow_port_forward` / label-ui's port** — MLflow's UI is at
`http://localhost:5000` after `kubectl port-forward -n sentinel-monitoring
svc/mlflow 5000:5000`; the labelling UI is at `http://localhost:8001` via
`kubectl port-forward -n sentinel-app svc/label-ui 8001:8001`. Both are
symmetric (same port on both sides) — 5000 and 8001 were simply free.

---

## State files

```
terraform.tfstate         — current state after last apply
terraform.tfstate.backup  — state before the last apply (one-step rollback)
*.backup                  — older backups with numeric timestamps
```

**Never edit the state file manually.** If state gets out of sync with reality
(e.g., you manually deleted a K8s resource that Terraform thinks exists), use:
```bash
terraform state rm kubernetes_deployment.mongo_express
```

to remove the stale entry from state without destroying the resource. Then run
`terraform import` or `terraform apply` to resync.

**State file contains secrets in plaintext** — the `postgres_password`,
`mongodb_password`, etc. are all in the state file. This is a Terraform design
constraint. Options for production:
- Use a remote backend with encryption (Terraform Cloud, S3 + KMS).
- Use `sensitive = true` on all secret variables (already done here — this
  only redacts output, not storage).
- Use Vault or AWS Secrets Manager and reference secrets by ID, not value.

---

## Common Terraform operations

```bash
# Preview changes without applying
terraform plan

# Apply with auto-approve (used by dev-start.sh)
terraform apply -auto-approve

# Override a variable for one apply
terraform apply -var="kafka_storage_size=5Gi"

# See current state
terraform show

# Remove a stuck resource from state (doesn't delete the K8s resource)
terraform state rm kubernetes_stateful_set.kafka

# List all resources Terraform is tracking
terraform state list

# Force-recreate a resource that's in a bad state
terraform taint kubernetes_job_v1.kafka_topic_init
terraform apply

# Destroy everything (WARNING: deletes all PVCs — all data lost)
terraform destroy
```

---

## Tips and tricks

**Checking why a pod won't start:**
```bash
kubectl describe pod -n sentinel-data -l app=kafka
kubectl logs -n sentinel-data statefulset/kafka --previous  # last crash logs
```

**Resizing a PVC** (e.g., Kafka data filling up):
1. Delete the StatefulSet without deleting the pod: `kubectl delete sts kafka -n sentinel-data --cascade=orphan`
2. Edit the PVC: `kubectl patch pvc data-kafka-0 -n sentinel-data -p '{"spec": {"resources": {"requests": {"storage": "5Gi"}}}}'`
3. Update `kafka_storage_size` variable.
4. `terraform apply` — recreates the StatefulSet with the new size.

**Refreshing a resource without recreating:**
```bash
terraform refresh  # syncs state from cluster reality without applying changes
```

**Why the Terraform kubernetes provider sometimes times out:**
`wait_for_rollout = true` blocks until the rollout completes. If a pod is in
CrashLoopBackOff, Terraform waits until the timeout, then fails. Check pod logs
in another terminal while Terraform is waiting. Common causes:
- Wrong secret value (check the secret with `kubectl get secret ... -o jsonpath='{.data}'`)
- Image pull failure (check with `kubectl describe pod ...`)
- ConfigMap content error (invalid YAML/SQL)
