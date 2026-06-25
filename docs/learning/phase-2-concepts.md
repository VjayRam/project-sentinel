# Sentinel — Phase 2 Concepts, Tricks & Tips
# Helm + PostgreSQL + MongoDB

Everything from deploying the databases module — Helm chart mechanics,
OCI registries, Bitnami patterns, and PostgreSQL internals.

---

## 1. What Helm Is

Helm is a package manager for Kubernetes. A **chart** is a bundle of templated
Kubernetes YAML files (Deployment, Service, PVC, ConfigMap, etc.) with a
`values.yaml` that lets you customise the deployment without modifying the templates.

```
Chart (templates + default values)
      +
Your values (overrides)
      │
      ▼
helm install → rendered YAML → kubectl apply → running pods
```

Without Helm, deploying PostgreSQL means writing and maintaining ~8 Kubernetes
manifests yourself. With Helm, it's one command. The tradeoff: you get less
visibility into what's happening unless you understand chart internals.

**A chart release** is a named instance of a chart deployed into a namespace.
You can have multiple releases of the same chart with different names and values.
`helm list -n sentinel-data` shows all releases in a namespace.

---

## 2. Helm in Terraform: `helm_release`

The Terraform Helm provider wraps `helm install`/`helm upgrade` as a resource:

```hcl
resource "helm_release" "postgresql" {
  name      = "postgresql"                                        # release name
  chart     = "oci://registry-1.docker.io/bitnamicharts/postgresql"  # chart source
  namespace = var.namespace                                       # target namespace
  wait      = true                                               # block until pods Ready
  timeout   = 300                                                # max seconds to wait

  values = [yamlencode({
    auth = { username = "sentinel", password = var.postgres_password }
  })]
}
```

When Terraform applies this:
1. Calls `helm install postgresql <chart> -n sentinel-data --values <rendered yaml>`
2. With `wait = true`, polls until all pods in the release pass their readiness probes
3. Records the release in state — future applies use `helm upgrade` if values change

**Trick:** `terraform destroy` runs `helm uninstall` — it deletes the Helm release
AND all the Kubernetes resources the chart created (pods, services, PVCs). Be
careful: PVCs are deleted by default, which means data loss. In production, use
`keep_history = true` and a separate PVC lifecycle policy.

---

## 3. OCI Registries vs HTTPS Helm Repos

The old way (pre-2022):
```hcl
repository = "https://charts.bitnami.com/bitnami"
chart      = "postgresql"
```
Helm fetches an `index.yaml` file from the repo, then downloads the chart tarball.

The new way (OCI, post-2022):
```hcl
chart = "oci://registry-1.docker.io/bitnamicharts/postgresql"
# No repository field — the full location is in the chart URI
```
OCI uses the same protocol as Docker image registries. The chart is stored as an
OCI artifact (like a container image). No `index.yaml` — the registry handles
discovery. Bitnami migrated to OCI in November 2022 because OCI provides better
security (image signing), better caching (layer deduplication), and a single
protocol instead of two.

**When you'll hit this:** any Bitnami chart installed via HTTPS after mid-2023
may fail with `invalid_reference: invalid tag` because the HTTPS repo is no longer
actively updated. The fix is always the same — switch to the OCI URI.

**Other OCI chart registries you'll encounter:**
- Bitnami: `oci://registry-1.docker.io/bitnamicharts/<chart>`
- AWS: `oci://public.ecr.aws/aws-controllers-k8s/<chart>`

---

## 4. Passing Values to Helm Charts

### Option A: multiple `set {}` blocks (verbose, hard to read)
```hcl
set { name = "auth.username", value = "sentinel" }
set { name = "auth.password", value = "secret" }
set { name = "primary.persistence.size", value = "2Gi" }
# ... 10+ more blocks
```

### Option B: `values = [yamlencode({...})]` (preferred)
```hcl
values = [yamlencode({
  auth = {
    username = "sentinel"
    password = var.postgres_password
  }
  primary = {
    persistence = { size = "2Gi" }
  }
})]
```

`yamlencode()` converts an HCL map/object to a YAML string. Terraform passes it
to Helm exactly as if you'd written a `values.yaml` file. The structure mirrors
the chart's `values.yaml` hierarchy — nested keys, not dotted paths.

**Why `yamlencode` wins:**
- The structure matches the chart documentation (which shows YAML, not dotted paths)
- You can collapse and expand sections visually
- `sensitive` variables in the map cause the entire `values` block to show as
  `(sensitive value)` in plan output — passwords never appear in terminal history

**Trick — see what values a chart accepts:**
```bash
helm show values oci://registry-1.docker.io/bitnamicharts/postgresql | less
```
This prints the full `values.yaml` with all defaults and comments. Everything you
can override with `yamlencode({})` is listed here.

---

## 5. Sensitive Values in Terraform

```hcl
variable "postgres_password" {
  type      = string
  sensitive = true   # ← this flag
}
```

Effect on plan output:
```
values = [(sensitive value)]   # instead of showing the actual YAML
```

Effect on `terraform output`:
```bash
terraform output postgres_password   # Error: Output refers to sensitive values
terraform output -json | jq          # Shows the value — intentional, requires explicit access
```

`sensitive = true` does NOT encrypt the value. It's still stored as plaintext in
`terraform.tfstate`. The flag only controls what gets printed to the terminal.

**For real secret management (beyond local dev):**
- Use `SENTINEL_DB_URL` as an environment variable at runtime (already done in the classifier)
- Use a Kubernetes Secret (the Bitnami chart creates one automatically)
- In cloud (Phase 6): use AWS Secrets Manager or GCP Secret Manager with Terraform's
  secrets data sources

**Trick — read the password Bitnami stored in a Kubernetes Secret:**
```bash
kubectl get secret postgresql -n sentinel-data -o jsonpath='{.data.password}' | base64 -d
```
Bitnami always creates a Secret named after the release. The `sentinel` user's
password is under the `password` key.

---

## 6. Helm Chart `wait` and Readiness Probes

```hcl
resource "helm_release" "postgresql" {
  wait    = true
  timeout = 300
}
```

`wait = true` tells the Helm provider to poll until all pods in the release have
their readiness probes passing. For PostgreSQL, the readiness probe is:
```
exec pg_isready -h localhost -U sentinel -d sentinel
```
The pod is Ready only when PostgreSQL is actually accepting connections — not just
when the process started.

Without `wait = true`, Terraform marks the Helm release as "created" the moment
Kubernetes accepts the install request. The pod might still be in `Pending` (image
pulling) or `Init:0/1` (waiting on initContainer). If a downstream resource tries
to connect to the DB before the pod is Ready, it fails.

**The sequencing this enables:**
```
Terraform apply:
  1. Create pg-schema-init ConfigMap          (instant)
  2. helm install postgresql [wait=true]       (blocks ~36s until pod Ready + initdb done)
  3. helm install mongodb    [wait=true]       (parallel with postgresql)
  4. ← returns only when both are Ready
```

The schema migration runs as part of step 2 — by the time Terraform finishes, the
tables exist and the database is ready to serve connections.

---

## 7. Bitnami's `initdb` Pattern

Bitnami's PostgreSQL chart runs SQL/shell scripts on **first boot only** — when
the data directory (`/bitnami/postgresql/data`) is empty.

```hcl
primary = {
  initdb = {
    scriptsConfigMap = "pg-schema-init"   # name of a ConfigMap in the same namespace
  }
}
```

The chart mounts the ConfigMap as a volume at `/docker-entrypoint-initdb.d/`. On
startup, PostgreSQL runs every `.sql` and `.sh` file in that directory in
alphabetical order.

**The `\connect sentinel;` requirement:**
Bitnami's initdb scripts execute as the `postgres` superuser against the `postgres`
default database. Our tables need to be in the `sentinel` database. Without
`\connect sentinel;` at the top of the SQL, the tables land in `postgres` instead.

```sql
\connect sentinel;   -- psql meta-command: switch to this database
-- Now all DDL runs in the sentinel database
CREATE TABLE IF NOT EXISTS classifications (...);
```

**Idempotency via `IF NOT EXISTS`:** all our `CREATE TABLE` and `CREATE INDEX`
statements use `IF NOT EXISTS`. If the pod ever restarts and somehow the initdb
runs again (shouldn't happen with persistent storage, but defensive coding), the
SQL is a no-op — no errors, no duplicate tables.

**Trick — verify the initdb ran correctly:**
```bash
kubectl exec -n sentinel-data postgresql-0 -- \
  env PGPASSWORD=sentinel psql -U sentinel -d sentinel -c '\dt'
```
You should see `classifications`, `drift_stats`, `model_registry`. If you see
them in the `postgres` database instead, the `\connect` line is missing.

---

## 8. StatefulSets vs Deployments for Databases

Bitnami deploys PostgreSQL as a **StatefulSet**, not a Deployment:
```
postgresql-0   (the "-0" suffix is always there for StatefulSets)
```

Key differences:

| | Deployment | StatefulSet |
|---|---|---|
| Pod names | `postgresql-abc123` (random) | `postgresql-0`, `postgresql-1` (stable, ordered) |
| Storage | Pods share a PVC OR each gets a new one | Each pod gets its own PVC, permanently bound |
| Startup order | All pods start in parallel | Pods start in order: 0, then 1, then 2 |
| Use case | Stateless services | Databases, queues, anything needing stable identity |

For a database, stable pod names matter because:
1. The service DNS (`postgresql.sentinel-data.svc.cluster.local`) routes to `postgresql-0`
2. The PVC `data-postgresql-0` is permanently bound to pod `postgresql-0`
3. If the pod restarts, it gets the same name and reattaches to the same PVC (same data)

A Deployment pod gets a random suffix on restart — it would mount a new empty PVC
(data loss).

---

## 9. PersistentVolumeClaims and StorageClasses

```yaml
# What Bitnami creates automatically:
kind: PersistentVolumeClaim
metadata:
  name: data-postgresql-0
spec:
  storageClassName: local-path   # we set this explicitly
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 2Gi
```

The StorageClass `local-path` (k3d's default) provisions a directory on the node's
filesystem under `/var/lib/rancher/k3s/storage/`. It's fast and works for local dev
but is not replicated — if the node dies, data is gone.

`ReadWriteOnce` means the PVC can only be mounted by **one pod at a time** on **one
node**. This is correct for databases. For shared read volumes (logs, model files),
you'd use `ReadOnlyMany` or `ReadWriteMany` — but `local-path` doesn't support those
(you'd need NFS or a cloud storage class).

**Trick — check what volumes are mounted:**
```bash
kubectl get pvc -n sentinel-data
# NAME                STATUS   VOLUME    CAPACITY   ACCESS MODES   STORAGECLASS
# data-postgresql-0   Bound    pvc-xxx   2Gi        RWO            local-path
# datadir-mongodb-0   Bound    pvc-yyy   2Gi        RWO            local-path
```

---

## 10. Connecting to Databases Inside the Cluster

From inside the cluster (another pod, like the classifier):
```
postgresql://sentinel:sentinel@postgresql.sentinel-data.svc.cluster.local:5432/sentinel
                                ^^^^^^^^^^^                                 ^^^^
                                service name (Bitnami names it after the release)
                                            ^^^^^^^^^^^^^^
                                            namespace
```

From outside (your laptop, using `kubectl exec`):
```bash
kubectl exec -n sentinel-data postgresql-0 -- \
  env PGPASSWORD=sentinel psql -U sentinel -d sentinel -c '\dt'
```

`env PGPASSWORD=sentinel` sets the environment variable for the psql process inside
the pod. Without it, psql prompts for a password — which doesn't work in
non-interactive `kubectl exec` commands.

**Service name convention:** Bitnami names the Service after the Helm release. If
the release name is `postgresql` and the chart name is `postgresql`, the chart's
`fullname` template sees that the release name already contains the chart name and
uses just the release name. Service = `postgresql`. If you'd named the release
`sentinel-pg`, the service would also be `sentinel-pg` (contains `pg` not `postgresql`,
so it would append: `sentinel-pg-postgresql`). This is why naming Helm releases
thoughtfully matters.

---

## Key Lessons Summary

| Concept | The lesson |
|---|---|
| Helm chart | Templated K8s manifests + values. One `helm install` = 8+ Kubernetes resources created automatically. |
| OCI vs HTTPS | Bitnami moved to OCI in 2022. Use `oci://registry-1.docker.io/bitnamicharts/<name>`, no `repository` field. |
| `yamlencode({})` | Converts HCL map to YAML for Helm values. Cleaner than `set {}` blocks; sensitive variables mask the whole block. |
| `sensitive = true` | Masks in plan/apply output only. Value is still plaintext in `terraform.tfstate` — don't commit the state file. |
| `wait = true` | Terraform blocks until all pods pass readiness probes. Without it, downstream resources may fail connecting to a DB that isn't ready. |
| Bitnami `initdb` | SQL files in `scriptsConfigMap` run on first boot only. Prepend `\connect <db>;` because scripts default to the `postgres` database. |
| StatefulSet | Databases need stable pod names and persistent PVC binding. A Deployment with a database is data loss waiting to happen. |
| `env PGPASSWORD=` | Pass passwords to `psql` in `kubectl exec` via env var — psql can't prompt for input in non-interactive mode. |
| Bitnami service naming | Release name = service name (when release name contains chart name). Name your releases intentionally. |
