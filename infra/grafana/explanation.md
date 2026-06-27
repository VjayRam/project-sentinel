# Grafana — Explanation

Grafana is a dashboarding and visualization layer. It does not store metrics —
it queries Prometheus (or other datasources) on demand and renders the results.
In Sentinel, Grafana runs as a Deployment inside the k3d cluster
(`sentinel-monitoring` namespace) and is pre-configured via the provisioning
system so no manual UI setup is required after `dev-start.sh`.

---

## Provisioning

Grafana has a provisioning system that reads YAML files from
`/etc/grafana/provisioning/` at startup and creates datasources, dashboards,
alert rules, and plugins without any UI interaction. Resources created by
provisioning:
- Cannot be accidentally deleted or modified through the UI (protected by
  `editable: false`).
- Are version-controlled alongside the code that generates the metrics.
- Are immediately available after `terraform apply` — no "first-time setup"
  step.

The directory structure mirrors Grafana's provisioning categories:
```
grafana/
  provisioning/
    datasources/
      prometheus.yml    ← what we have now
    dashboards/         ← add dashboard JSON files here (Phase 4+)
    alerting/           ← alert rules and notification policies
    plugins/            ← plugin installations
```

Grafana mounts this directory via a Kubernetes ConfigMap volume. Any change
to the YAML files requires a `kubectl rollout restart deployment/grafana` to
take effect (or a `terraform apply` if the ConfigMap resource changed).

---

## provisioning/datasources/prometheus.yml

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

### `access: proxy`

This is the most important setting. Grafana has two ways to reach a datasource:

- **`proxy`** (what we use): the browser sends a PromQL query to the Grafana
  backend, and the Grafana Go process forwards it to Prometheus. The browser
  never talks to Prometheus directly.
- **`direct`**: the browser talks to Prometheus directly. This only works if
  the user's browser can reach the Prometheus URL — which fails in any
  containerized setup because `http://prometheus:9090` is an in-cluster DNS
  name, not resolvable from a browser.

Always use `proxy` for cluster-internal datasources. The cosmetic "Failed to
fetch" health check warning you see in the Grafana UI is the browser trying a
direct health check — it's a known UI quirk and does not affect actual queries.

### `url: http://prometheus:9090`

Uses the Kubernetes Service DNS name `prometheus` in the `sentinel-monitoring`
namespace. From inside the cluster, this resolves to Prometheus's ClusterIP.
Since Grafana's pod is in the same namespace, the short name `prometheus` works.
For cross-namespace, you'd need `prometheus.sentinel-monitoring.svc.cluster.local`.

### `isDefault: true`

When a Grafana panel or Explore query doesn't specify a datasource, it uses the
default. With `isDefault: true`, all PromQL queries work without explicitly
selecting Prometheus each time.

### `editable: false`

Prevents the provisioned datasource from being saved with edits via the UI.
If a team member changes the URL or access mode in the UI, those changes
disappear on the next Grafana pod restart. All datasource config changes go
through the YAML file and a `terraform apply`.

**Tip:** temporarily set this to `true` while experimenting with a new
datasource configuration, then lock it back to `false` once you know what you
want and commit it to the file.

---

## Grafana's deployment in Terraform

Grafana in Sentinel is deployed as a Kubernetes Deployment (not StatefulSet)
with a ConfigMap for the provisioning files and a PersistentVolumeClaim for
user data (dashboards created via UI, user preferences, etc.).

Key configuration via Kubernetes env vars (which map to `grafana.ini` settings):

```hcl
env { name = "GF_SECURITY_ADMIN_USER";     value = "admin" }
env { name = "GF_SECURITY_ADMIN_PASSWORD"; value = var.grafana_admin_password }
env { name = "GF_USERS_ALLOW_SIGN_UP";     value = "false" }
env { name = "GF_AUTH_ANONYMOUS_ENABLED";  value = "false" }
env { name = "GF_DATASOURCES_DEFAULT_NAME"; value = "Prometheus" }
```

All Grafana configuration can be set this way — any `grafana.ini` key
`[section] key = value` maps to `GF_SECTION_KEY`. For example:
- `[server] domain = grafana.example.com` → `GF_SERVER_DOMAIN=grafana.example.com`
- `[smtp] enabled = true` → `GF_SMTP_ENABLED=true`

The `GF_DATASOURCES_DEFAULT_NAME=Prometheus` env var must match the `name`
field in the provisioned datasource YAML exactly (case-sensitive).

---

## Working with dashboards

### Creating a dashboard

1. Open `http://localhost:3000`, log in as `admin` / `admin`.
2. Click **Dashboards → New → New dashboard**.
3. Add panels using PromQL queries.
4. Save the dashboard.

Dashboards saved through the UI are stored in Grafana's SQLite database, which
lives in the `grafana_data` PVC. They survive pod restarts but are not in Git.

### Exporting a dashboard to Git

Once you've built a useful dashboard:
1. Open the dashboard → click the share icon → **Export → Export as JSON**.
2. Save the JSON to `infra/grafana/provisioning/dashboards/<name>.json`.
3. Add a dashboard provider to `provisioning/dashboards/default.yaml`:

```yaml
apiVersion: 1

providers:
  - name: sentinel
    folder: Sentinel
    type: file
    disableDeletion: true
    options:
      path: /etc/grafana/provisioning/dashboards
```

4. Update the Grafana ConfigMap in Terraform to include both the datasource and
   dashboards directory, and mount the JSON files.

After `terraform apply`, the dashboard appears under **Dashboards → Sentinel**
and is locked against UI edits (`disableDeletion: true`).

### Useful starter queries for Sentinel panels

```promql
# Real-time harm rate
sum(rate(classifier_requests_total{label="harm"}[2m]))
  / sum(rate(classifier_requests_total[2m]))

# P50 / P95 / P99 latency (from recording rules)
classifier:request_latency_p50:5m
classifier:request_latency_p95:5m
classifier:request_latency_p99:5m

# Throughput by endpoint
sum(rate(classifier_requests_total[5m])) by (endpoint)

# Model version currently serving (gauge showing distinct versions)
count by (model_version) (classifier_requests_total)

# Memory usage in MB
process_resident_memory_bytes{job="classifier"} / 1024 / 1024
```

### Dashboard variables (templating)

Grafana lets you define dashboard variables that act as dynamic filters. For
example, add a `model_version` variable:
1. Dashboard settings → Variables → Add variable.
2. Type: **Query**, Datasource: **Prometheus**.
3. Query: `label_values(classifier_requests_total, model_version)`.
4. Use it in panels as `$model_version`.

This lets you compare metrics across model versions without building separate
dashboards.

---

## Tips and tricks

**Hot reload after changing provisioning:**
Provisioning files are only read on startup. After editing
`provisioning/datasources/prometheus.yml`, reload Grafana:
```bash
kubectl rollout restart deployment/grafana -n sentinel-monitoring
```

**Check provisioning errors:**
```bash
kubectl logs -n sentinel-monitoring deployment/grafana | grep -i "provision"
```

**Explore datasource directly:**
Open `http://localhost:3000/explore`. Select the **Prometheus** datasource.
Run raw PromQL queries interactively. This is the fastest way to validate an
alert expression before writing it into a rule file.

**Grafana behind a reverse proxy:**
If you ever expose Grafana on a path prefix (e.g., `/grafana/`), add:
```
GF_SERVER_ROOT_URL=http://example.com/grafana/
GF_SERVER_SERVE_FROM_SUB_PATH=true
```

**Plugins:**
Add plugins via provisioning instead of clicking in the UI:
```yaml
# provisioning/plugins/default.yaml
apiVersion: 1
apps:
  - type: grafana-clock-panel
    disabled: false
```

Grafana downloads the plugin at startup. Useful for adding the `grafana-piechart-panel`,
`grafana-worldmap-panel`, or any community visualization.
