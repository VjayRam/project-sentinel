# Infra — Component Explanations

## Directory structure

```
infra/
  prometheus/
    prometheus.yml              — Prometheus global config, scrape jobs, rule file references
    rules/
      classifier.yml            — Alert rules and recording rules for the classifier service
  grafana/
    provisioning/
      datasources/
        prometheus.yml          — Auto-provision Prometheus as Grafana's default datasource

docker-compose.yml              — Brings up Prometheus and Grafana as local containers
```

---

## docker-compose.yml

The compose file is the single command to bring the entire observability stack up locally. It defines two services: `prometheus` and `grafana`.

### Prometheus service

```yaml
image: prom/prometheus:v3.4.1
ports:
  - "9090:9090"
volumes:
  - ./infra/prometheus:/etc/prometheus:ro
  - prometheus_data:/prometheus
command:
  - "--config.file=/etc/prometheus/prometheus.yml"
  - "--storage.tsdb.path=/prometheus"
  - "--storage.tsdb.retention.time=7d"
  - "--web.enable-lifecycle"
  - "--web.enable-remote-write-receiver"
extra_hosts:
  - "host.docker.internal:host-gateway"
restart: unless-stopped
```

**Image pinned to `v3.4.1`** — never use `latest` in any infrastructure config. `latest` changes silently on the next `docker pull`, making environments non-reproducible.

**Volume mounts:**
- `./infra/prometheus:/etc/prometheus:ro` — mounts the entire `infra/prometheus/` directory into the container. The `:ro` flag makes it read-only inside the container; Prometheus reads config but never writes to it.
- `prometheus_data:/prometheus` — a named Docker volume for TSDB (time series database) storage. Named volumes persist across `docker compose down` and are managed by Docker. If you run with `-v` (`docker compose down -v`), this volume is deleted and all metric history is lost.

**CLI flags:**
- `--config.file` — tells Prometheus where to read its config. This matches the mount point.
- `--storage.tsdb.path` — where the TSDB writes data. Using a named volume here keeps data outside the container filesystem.
- `--storage.tsdb.retention.time=7d` — deletes data older than 7 days. Prevents the TSDB from growing unboundedly. For production, you typically set this to 15d–30d and rely on remote write to long-term storage (Thanos, Cortex, Mimir) for anything longer.
- `--web.enable-lifecycle` — enables the `POST /-/reload` endpoint, which re-reads the config file without restarting the container. Use this after editing `prometheus.yml` or `rules/classifier.yml`.
- `--web.enable-remote-write-receiver` — enables Prometheus to accept remote write payloads at `/api/v1/write`. Useful when other services (like the stream processor) want to push custom metrics rather than exposing a scrape endpoint.

**`extra_hosts: host.docker.internal:host-gateway`** — on Linux, Docker containers cannot reach the host machine via `host.docker.internal` by default (unlike Docker Desktop on Mac/Windows, which sets this automatically). Adding `host-gateway` as the resolution maps `host.docker.internal` to the Docker bridge IP (typically `172.17.0.1`), which is reachable from inside the container. This is how Prometheus scrapes the classifier service, which runs directly on the host (not in a container) during local development. Without this, scrapes to `host.docker.internal:8000` would time out.

### Grafana service

```yaml
image: grafana/grafana:12.0.2
ports:
  - "3000:3000"
environment:
  - GF_SECURITY_ADMIN_USER=admin
  - GF_SECURITY_ADMIN_PASSWORD=sentinel
  - GF_USERS_ALLOW_SIGN_UP=false
  - GF_DATASOURCES_DEFAULT_NAME=Prometheus
volumes:
  - grafana_data:/var/lib/grafana
  - ./infra/grafana/provisioning:/etc/grafana/provisioning:ro
depends_on:
  - prometheus
```

**Environment variables** — Grafana reads all `GF_*` env vars as config overrides, which corresponds to properties in `grafana.ini`. This is the standard pattern for containerized Grafana: no need to bake a custom `grafana.ini` just to set the admin password.

- `GF_SECURITY_ADMIN_USER / GF_SECURITY_ADMIN_PASSWORD` — the initial admin credentials. Change the password before exposing this to a network.
- `GF_USERS_ALLOW_SIGN_UP=false` — disables the public sign-up page. Anyone with the URL cannot self-register.
- `GF_DATASOURCES_DEFAULT_NAME=Prometheus` — sets the default datasource name (matches what the provisioning file creates below).

**`depends_on: prometheus`** — Docker Compose starts Prometheus before Grafana. This is a startup ordering dependency, not a health check — Grafana will attempt to connect to Prometheus as soon as it boots, but if Prometheus hasn't finished loading config yet, Grafana retries automatically.

**`grafana_data:/var/lib/grafana`** — persists dashboards, users, and alerts across restarts.

**`./infra/grafana/provisioning:/etc/grafana/provisioning:ro`** — Grafana reads this directory at startup and auto-provisions the contents. Grafana supports provisioning for datasources, dashboards, alert rules, and plugins. Provisioned resources cannot be edited through the UI (by design — they're managed as code).

### Named volumes

```yaml
volumes:
  prometheus_data:
  grafana_data:
```

Declaring volumes at the top level makes them Docker-managed named volumes (not bind mounts). They survive `docker compose down` but are deleted by `docker compose down -v`. Named volumes are the correct choice for database/TSDB storage because they avoid file permission issues that bind mounts can cause on Linux.

---

## infra/prometheus/prometheus.yml

The main Prometheus configuration file. Prometheus re-reads this file on `POST /-/reload` (or SIGHUP). It does not restart the process.

### Global block

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    environment: local
```

`scrape_interval: 15s` — how often Prometheus pulls metrics from each target. The classifier scrape job overrides this to `10s` for finer granularity.

`evaluation_interval: 15s` — how often Prometheus evaluates the rules in `classifier.yml`. Recording rules and alert rules are re-evaluated on every tick. Setting this equal to `scrape_interval` means alerts see data that is at most one interval old.

`external_labels: environment: local` — attached to every time series and alert that Prometheus sends to Alertmanager or remote storage. When you add cloud environments (Phase 8), change this to `environment: staging` or `environment: production`. This label lets you filter all data by environment in Grafana across multiple Prometheus instances federated together.

### Alerting block

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: []
```

Empty target list means no Alertmanager is wired up yet. Alerts fire (you can see them in Prometheus UI at `/alerts`) but are not routed anywhere. In production you point this at an Alertmanager instance (PagerDuty, Slack, email routing). Phase 4 adds Alertmanager to the compose stack.

### Rule files

```yaml
rule_files:
  - rules/classifier.yml
```

Prometheus loads all files matching this glob pattern. Add more rule files here as more services are added (`rules/kafka.yml`, `rules/drift.yml`, etc.). Each file is independent — an error in one does not break loading of others.

### Scrape jobs

#### `prometheus` job

```yaml
- job_name: prometheus
  static_configs:
    - targets: ["localhost:9090"]
```

Prometheus scrapes itself. This is standard practice — it lets you monitor Prometheus's own health: whether its TSDB is filling up, how long rule evaluation takes, whether scrapes are timing out. The `prometheus_tsdb_head_chunks` and `prometheus_rule_evaluation_duration_seconds` metrics come from this self-scrape.

#### `classifier` job

```yaml
- job_name: classifier
  metrics_path: /metrics/
  scrape_interval: 10s
  static_configs:
    - targets: ["host.docker.internal:8000"]
  relabel_configs:
    - target_label: service
      replacement: classifier
```

`metrics_path: /metrics/` — FastAPI mounts the Prometheus ASGI app at `/metrics` but redirects `/metrics` → `/metrics/` (trailing slash). If you use `/metrics` without the trailing slash, Prometheus follows the redirect, but the redirect itself is a 301 and adds latency. Setting the path directly avoids the round-trip.

`scrape_interval: 10s` — overrides the global 15s for the classifier job. The classifier is the primary service being monitored; more frequent scrapes reduce the gap between a spike and when it appears in Prometheus.

`targets: ["host.docker.internal:8000"]` — the classifier runs on the host, not in a container. See the `extra_hosts` note above for why this resolves correctly.

**`relabel_configs`** — relabeling is one of the most powerful features in Prometheus. It runs on each scraped target's labels before the time series are ingested.

```yaml
relabel_configs:
  - target_label: service
    replacement: classifier
```

This adds `service="classifier"` to every metric from this job. Without this, you'd filter by `job="classifier"` in PromQL — both work, but `service` is a more semantically clear label that matches what you'll use in production (Kubernetes pod labels, Datadog tags, etc.). It also means Grafana dashboards can filter by `service` consistently across all data sources.

---

## infra/prometheus/rules/classifier.yml

Alert and recording rules for the classifier service. Prometheus evaluates these every `evaluation_interval` (15s). Rules are organized into five named groups.

### How Prometheus rules work

**Recording rules** compute a new metric from existing metrics and store it as a new time series. They exist for two reasons:
1. Performance: expensive queries (like `histogram_quantile`) are computed once by Prometheus and stored, rather than computed at query time by every Grafana panel refresh.
2. Reuse: a recording rule named `classifier:request_latency_p95:5m` can be referenced in alert expressions and dashboard panels without duplicating the full PromQL expression.

**Alert rules** evaluate a PromQL expression and fire when it is true for a specified duration (`for`). The `for` duration prevents transient spikes from creating noisy alerts — an alert only fires if the condition holds continuously.

### `classifier.health` group

```yaml
- alert: ClassifierDown
  expr: up{job="classifier"} == 0
  for: 1m
```

`up{job="classifier"}` is a special metric Prometheus generates from its own scrapes. It is `1` if the last scrape succeeded, `0` if it failed. A scrape fails when Prometheus cannot reach the target at all (connection refused, DNS failure) or when it times out. `for: 1m` means the target must be unreachable for a full minute before the alert fires — avoids false positives from a single missed scrape.

```yaml
- alert: ClassifierNoTraffic
  expr: rate(classifier_requests_total[5m]) == 0
  for: 10m
```

Fires if the service has received no requests for 10 minutes. This is different from `ClassifierDown` — the service is reachable (scrapes succeed) but no traffic is flowing through it. Usually indicates upstream disconnection (stream processor stopped sending) or a routing issue (traffic going to a different service version).

### `classifier.latency` group

**Recording rules:**

```yaml
- record: classifier:request_latency_p50:5m
  expr: histogram_quantile(0.50, rate(classifier_request_latency_seconds_bucket[5m]))
```

`histogram_quantile` is expensive to compute because it iterates over all bucket time series. Pre-computing it as a recording rule means Grafana dashboards and alert rules can reference `classifier:request_latency_p50:5m` as a simple metric lookup rather than re-running the full quantile computation on every panel refresh.

**Naming convention** — `namespace:metric:aggregation_window` is the standard Prometheus recording rule naming convention:
- `classifier` = service namespace
- `request_latency_p50` = what is being measured
- `5m` = the range window used in the expression

**Alert rules:**

```yaml
- alert: ClassifierHighLatencyP95
  expr: classifier:request_latency_p95:5m > 0.2
  for: 2m
```

The threshold `0.2` is in seconds (200ms). The INT8 model's expected p50 is ~35ms; p95 at 200ms indicates something is wrong (queue backup, CPU saturation, cold JVM/ORT). `for: 2m` avoids alerting on brief latency spikes from garbage collection or OS scheduling.

```yaml
- alert: ClassifierHighLatencyP99
  expr: classifier:request_latency_p99:5m > 0.5
  for: 2m
  labels:
    severity: critical
```

500ms p99 is the SLA breach threshold. `critical` means this would page on-call in production. Separate p95 (warning) and p99 (critical) thresholds because p99 latency is driven by different causes than p95 — often large batches, GC pauses, or memory pressure.

### `classifier.throughput` group

```yaml
- record: classifier:request_rate:5m
  expr: sum(rate(classifier_requests_total[5m])) by (endpoint)
```

Computes per-endpoint request rate (requests per second). Summing by `endpoint` gives two series: one for `/classify` and one for `/classify/batch`. This is the primary throughput metric for capacity planning — if the rate exceeds the service's sustainable throughput, you add replicas.

```yaml
- alert: ClassifierBatchBackpressure
  expr: histogram_quantile(0.90, rate(classifier_batch_size_bucket[5m])) >= 60
  for: 5m
```

The `classifier_batch_size` histogram tracks how many texts are in each `/classify/batch` call (not the dynamic batcher's internal batches). If the p90 batch size is ≥60 (out of a max of 64) for 5 minutes, clients are consistently sending near-maximum batches. This signals that the service is approaching capacity — either raise `MAX_BATCH_SIZE`, add replicas, or investigate why clients are batching so aggressively.

### `classifier.logs` group

```yaml
- record: classifier:log_error_rate:5m
  expr: rate(classifier_log_errors_total[5m])
```

The rate of log records at ERROR or CRITICAL level, per second, from the `_PrometheusLogHandler` in `metrics.py`. This is the bridge between Python's `logging` module and Prometheus metrics.

```yaml
- alert: ClassifierLogErrors
  expr: rate(classifier_log_errors_total{level="ERROR"}[5m]) > 0.1
  for: 2m
```

More than 0.1 errors per second (1 error per 10 seconds) for 2 minutes. This is a noisy signal by itself — many errors can be transient (network retries, bad requests). The `for: 2m` requirement ensures it's a sustained pattern, not a burst.

```yaml
- alert: ClassifierCriticalErrors
  expr: rate(classifier_log_errors_total{level="CRITICAL"}[5m]) > 0
  for: 0m
```

`for: 0m` means this alert fires immediately on the first CRITICAL log record. There is no waiting period — a CRITICAL log is, by definition, a condition requiring immediate investigation. In production this would trigger a PagerDuty page.

### `classifier.resources` group

```yaml
- alert: ClassifierHighMemory
  expr: process_resident_memory_bytes{job="classifier"} > 2 * 1024 * 1024 * 1024
  for: 5m
```

`process_resident_memory_bytes` is an auto-generated metric from `prometheus_client` (part of the default Python process metrics). It measures RSS (resident set size) — the actual physical RAM the process is using. The INT8 model is 120 MB on disk but expands to roughly 500 MB in memory after ORT loads it. A sustained RSS above 2 GB indicates a memory leak (common when asyncio Futures are not resolved) or unexpected model reloading.

```yaml
- alert: ClassifierFdLeak
  expr: process_open_fds{job="classifier"} > 200
  for: 5m
```

`process_open_fds` counts open file descriptors. Normal classifier operation uses a small number of fds (network sockets, the ONNX file, log files). More than 200 sustained for 5 minutes indicates a leak — typically unclosed HTTP connections or log file handles. On Linux, the default per-process fd limit is 1024; a leak reaching 200 gives early warning before the process hits the OS limit and starts failing with `Too many open files`.

---

## infra/grafana/provisioning/datasources/prometheus.yml

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

**Provisioning** — Grafana reads files in `/etc/grafana/provisioning/datasources/` at startup and creates datasources from them. This replaces the manual "Add datasource" UI flow. The datasource exists immediately after `docker compose up`, with no manual setup needed.

**`access: proxy`** — Grafana's backend (the Go process) proxies queries to Prometheus on behalf of the browser. The alternative is `direct` (the browser talks to Prometheus directly), which fails in containerized setups where the browser cannot reach `http://prometheus:9090` — that hostname only resolves inside the Docker network.

**`url: http://prometheus:9090`** — uses the Docker Compose service name `prometheus` as the hostname. Within the Docker network, service names are DNS-resolvable. This is why the compose file has `depends_on: prometheus` on Grafana — Grafana must be able to resolve `prometheus` at startup.

**`isDefault: true`** — marks this as the datasource that Grafana uses when no datasource is explicitly selected. PromQL queries in the Explore UI and dashboards that don't specify a datasource will use this.

**`editable: false`** — prevents the provisioned datasource from being modified through the Grafana UI. If a team member edits it via the UI, their changes are overwritten on the next Grafana restart. This enforces infrastructure-as-code: changes to the datasource go through Git, not UI clicks.

---

## How the components connect

```
Host machine
  classifier (uvicorn :8000)
    ↑  /metrics/ exposed via make_asgi_app()

Docker network
  prometheus (:9090)
    → scrapes host.docker.internal:8000/metrics/ every 10s
    → evaluates rules/classifier.yml every 15s
    → fires alerts to Alertmanager (not wired yet)
    ↑  self-scrapes localhost:9090

  grafana (:3000)
    → queries prometheus:9090 via proxy
    ← provisioned datasource from infra/grafana/provisioning/
```

The classifier runs outside Docker. Prometheus runs inside Docker but reaches the host via `host.docker.internal` (mapped via `extra_hosts: host-gateway`). Grafana reaches Prometheus via the Docker network's internal DNS (`prometheus:9090`).
