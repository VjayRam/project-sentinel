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

```hcl
env { name = "KAFKA_CFG_PROCESS_ROLES";    value = "broker,controller" }
env { name = "KAFKA_CFG_CONTROLLER_QUORUM_VOTERS"; value = "1@localhost:9093" }
```

**KRaft mode** (no ZooKeeper) — Kafka 3.3+ includes KRaft as production-stable.
One process handles both the broker role (serving producers/consumers) and the
controller role (managing cluster metadata, topic assignments, leader elections).
For a single-node local setup, `localhost:9093` works for the controller quorum
voter because the controller and broker are in the same pod.

**Two listeners:**

```hcl
env { name = "KAFKA_CFG_LISTENERS";
      value = "PLAINTEXT://:9092,CONTROLLER://:9093,EXTERNAL://:9094" }
env { name = "KAFKA_CFG_ADVERTISED_LISTENERS";
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

**`KAFKA_CFG_OFFSETS_TOPIC_REPLICATION_FACTOR=1`** — the `__consumer_offsets`
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
