# Prometheus — Explanation

Prometheus is a pull-based metrics system. It periodically connects to each
configured target, fetches the metrics endpoint, parses the exposition format,
and stores the result as time series in its TSDB. It also evaluates rules on a
timer and optionally routes alerts to Alertmanager.

In Sentinel, Prometheus runs as a StatefulSet inside the k3d cluster
(`sentinel-monitoring` namespace) and scrapes the classifier service running
locally on the host machine.

---

## prometheus.yml

### Global block

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    environment: local
```

**`scrape_interval: 15s`** — how often Prometheus contacts each target. Lower
values give finer temporal resolution but increase load on both Prometheus and
the target. Individual jobs can override this (the classifier uses 10s).

**`evaluation_interval: 15s`** — how often alert and recording rules are
re-evaluated. Setting this equal to `scrape_interval` means rules always see
the freshest data — a rule can never be evaluated against data older than one
interval.

**`external_labels`** — these labels are appended to every time series and
every alert that Prometheus sends outward (to Alertmanager, remote write,
federation). When you add a staging/production cluster in Phase 8, change
`environment` to `staging` or `production` so you can filter Grafana dashboards
across environments from a single pane of glass.

**Tweak:** drop the classifier's `scrape_interval` to `5s` when debugging a
latency spike. You get 3× more data points. Revert afterward — 5s means
Prometheus fires 12 connections per minute at the classifier.

---

### Alerting block

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: []
```

Empty `targets` means no Alertmanager is connected. Alerts still fire and are
visible at `http://localhost:9090/alerts`, but they are not routed anywhere
(no Slack message, no PagerDuty page). This is intentional for local dev.

When you wire up Alertmanager (Phase 7+), replace the empty list with the
Alertmanager service address:
```yaml
targets: ["alertmanager.sentinel-monitoring.svc.cluster.local:9093"]
```

**Tip:** visit `http://localhost:9090/alerts` now to see which alerts are
currently firing. `ClassifierNoTraffic` will fire if the stream processor
hasn't sent any requests recently — that's normal during initial setup.

---

### Rule files

```yaml
rule_files:
  - rules/classifier.yml
```

Prometheus loads all files matching each glob pattern. You can add per-service
rule files as the system grows:
```yaml
rule_files:
  - rules/classifier.yml
  - rules/kafka.yml        # Phase 5 — add when you instrument the consumer
  - rules/drift.yml        # Phase 6 — add when Spark jobs emit metrics
```

An error in one file does not prevent others from loading. Prometheus logs a
warning and continues. Check `http://localhost:9090/config` to see which rule
files are loaded, and `http://localhost:9090/rules` to see every rule's current
state and last evaluation result.

---

### Scrape jobs

#### `prometheus` job

```yaml
- job_name: prometheus
  static_configs:
    - targets: ["localhost:9090"]
```

Prometheus scrapes itself. This gives you Prometheus's own health as a
first-class metric:

- `prometheus_tsdb_head_samples_appended_total` — ingestion rate
- `prometheus_rule_evaluation_duration_seconds` — how long rules take to evaluate
- `prometheus_target_scrape_duration_seconds` — how long each scrape takes
- `prometheus_tsdb_head_chunks` — how much data is in memory (watch this for memory growth)

**Tip:** in Grafana's Explore view, run `up` to see all currently healthy
targets. A value of `1` means the last scrape succeeded; `0` means it failed.

---

#### `classifier` job

```yaml
- job_name: classifier
  metrics_path: /metrics/
  scrape_interval: 10s
  static_configs:
    - targets: ["host.k3d.internal:8000"]
  relabel_configs:
    - target_label: service
      replacement: classifier
```

**`host.k3d.internal`** — k3d injects this hostname into the `/etc/hosts` of
every pod it manages. It resolves to the Docker bridge IP, which routes to the
host machine. This is how Prometheus (running inside k3d) reaches the classifier
(running on the host via `dev-start.sh`) without any port-forward tricks.

`host.docker.internal` is the equivalent for plain Docker / Docker Desktop.
k3d uses `host.k3d.internal` because k3d is built on top of Docker's network
model but manages its own `/etc/hosts` injection.

**`metrics_path: /metrics/`** — the trailing slash is required. FastAPI mounts
the Prometheus ASGI app at `/metrics` and responds with a redirect to
`/metrics/`. If you omit the slash, Prometheus follows the 301 redirect on
every scrape, adding a round-trip. Specifying the final path directly avoids it.

**`relabel_configs`** — relabeling runs on each scraped target's label set
before time series are stored. This is one of the most powerful features in
Prometheus.

The rule here adds a `service="classifier"` label to every time series from
this job. Without it, you'd filter by `job="classifier"` in PromQL, which works
but is less semantic. Consistent `service` labels across all your data sources
(Prometheus, Loki, Tempo) enable unified filtering in Grafana.

**Other useful relabel operations:**

```yaml
# Drop a metric entirely (don't store it)
- source_labels: [__name__]
  regex: "go_gc_.*"
  action: drop

# Rename a label
- source_labels: [pod]
  target_label: k8s_pod
  action: replace

# Add a static label based on a URL segment
- source_labels: [__address__]
  regex: "([^:]+):.*"
  target_label: host
  replacement: "$1"
```

**Tip:** add a second classifier target to scrape both the local classifier and
a staging one simultaneously:
```yaml
static_configs:
  - targets: ["host.k3d.internal:8000"]
    labels:
      env: local
  - targets: ["staging-host:8000"]
    labels:
      env: staging
```

---

## rules/classifier.yml

### How rule evaluation works

Prometheus evaluates every rule group every `interval` seconds. Within a group,
rules execute sequentially in order — later rules can reference metrics produced
by earlier recording rules in the same group.

**Recording rules** store a new time series under the given `record` name. They
exist because:
1. **Performance**: `histogram_quantile` over a histogram with 20+ buckets is
   expensive. Pre-computing it once per interval is far cheaper than recomputing
   it on every Grafana panel refresh.
2. **Clarity**: `classifier:request_latency_p95:5m` in a PromQL expression is
   self-documenting; the full `histogram_quantile(0.95, rate(...))` is not.
3. **Reuse**: the same pre-computed metric can be used in multiple alert
   expressions without duplicating the PromQL.

**Alert rules** evaluate a boolean PromQL expression. When it is true, the alert
enters the `PENDING` state. If it stays true continuously for the `for` duration,
it becomes `FIRING`. If the condition clears before `for` expires, it resets to
`INACTIVE`. The `for` clause is the primary noise-reduction knob.

---

### Group: `classifier.health`

```yaml
- alert: ClassifierDown
  expr: up{job="classifier"} == 0
  for: 1m
```

`up` is generated by Prometheus itself, not by the classifier. It is `1` when
the last scrape returned HTTP 200 with valid metrics, `0` for any failure
(connection refused, timeout, parse error). `for: 1m` tolerates a single missed
scrape (15s) plus any brief restart.

```yaml
- alert: ClassifierNoTraffic
  expr: rate(classifier_requests_total[5m]) == 0
  for: 10m
```

A different failure mode from `ClassifierDown` — the classifier is up and
responding to Prometheus scrapes, but no inference requests are flowing through
it. Most likely cause in Phase 5: stream processor is stopped or the Kafka
consumer is not committing (stuck in retry loop).

**Tip:** `rate(metric[5m]) == 0` is not the same as `absent(metric)`. `absent`
fires if the metric has never existed or hasn't been updated recently. `rate ==
0` fires if the metric exists but the counter has not incremented. Use `absent`
to catch cases where the classifier crashed and its metrics disappeared entirely.

---

### Group: `classifier.latency`

```yaml
- record: classifier:request_latency_p50:5m
  expr: histogram_quantile(0.50, rate(classifier_request_latency_seconds_bucket[5m]))
```

The `classifier_request_latency_seconds` histogram has multiple bucket time
series (one per le= threshold). `rate(...[5m])` computes per-second rate of
observations falling into each bucket over 5 minutes. `histogram_quantile`
interpolates the quantile from those rates.

**Why `rate` instead of raw counters?** The buckets are monotonically increasing
counters. `rate` converts them to per-second rates, which stay stable as traffic
increases. Without `rate`, `histogram_quantile` would compute quantiles over the
lifetime of the process, not the recent window.

**`[5m]` range window trade-off:**
- Shorter window (1m, 2m): faster to react to latency spikes, but noisier.
- Longer window (10m, 15m): smoother signal, but slow to detect regressions.
- 5m is the standard starting point. Adjust based on your traffic variability.

The three recording rules (p50, p95, p99) give you a full latency profile:
- p50 = median, roughly "typical" request
- p95 = the experience of 1 in 20 requests — your practical SLA
- p99 = worst-case tail — driven by outliers (large batches, GC pauses)

```yaml
- alert: ClassifierHighLatencyP95
  expr: classifier:request_latency_p95:5m > 0.2
  for: 2m
```

**`0.2` = 200ms.** The INT8 model's expected p95 in single-request mode is
~50ms. 200ms allows 4× headroom for load, batching, and ORT warmup variation.
Hitting 200ms p95 consistently means something structural is wrong.

**`for: 2m`** — 2 minutes of sustained high latency is the threshold. A brief
spike from a GC pause or cold start does not page anyone.

**Tweak these thresholds for your hardware.** Run the classifier under load with
`scripts/simulate-traces.py --count 500 --batch 10` and observe the actual p95
before setting alert thresholds.

---

### Group: `classifier.throughput`

```yaml
- record: classifier:request_rate:5m
  expr: sum(rate(classifier_requests_total[5m])) by (endpoint)
```

`sum(...) by (endpoint)` produces one time series per endpoint label value
(`/classify` and `/classify/batch`). This lets you see throughput split by
endpoint, which is critical when diagnosing whether a throughput drop is from
the single-request path or the batch path.

```yaml
- alert: ClassifierBatchBackpressure
  expr: histogram_quantile(0.90, rate(classifier_batch_size_bucket[5m])) >= 60
  for: 5m
```

`classifier_batch_size` is a histogram of how many texts are in each
`/classify/batch` call. The max is 64 (`MAX_BATCH_SIZE` in schemas.py). If the
p90 is ≥60 for 5 minutes, clients are consistently pushing batches to the limit.

This can mean two things:
1. The stream processor is batching aggressively because it's falling behind
   Kafka (good — it's using the batch endpoint efficiently).
2. The service is saturated and the batch endpoint is the bottleneck.

Look at latency alongside this alert. High batch size + low latency = healthy.
High batch size + high latency = scale or optimize.

---

### Group: `classifier.logs`

```yaml
- record: classifier:log_error_rate:5m
  expr: rate(classifier_log_errors_total[5m])
```

`classifier_log_errors_total` is incremented by `_PrometheusLogHandler` in
`services/classifier/metrics.py` every time Python's `logging` module emits an
ERROR or CRITICAL record. This bridges the structured log world and the metrics
world — you can alert on log error rates without shipping logs to a log
aggregator.

```yaml
- alert: ClassifierCriticalErrors
  expr: rate(classifier_log_errors_total{level="CRITICAL"}[5m]) > 0
  for: 0m
```

`for: 0m` is the only place this matters — it fires immediately on the first
CRITICAL log, with no waiting period. CRITICAL logs in the classifier indicate
unhandled exceptions during startup or catastrophic model failures. There is no
reason to wait before alerting on these.

---

### Group: `classifier.resources`

```yaml
- alert: ClassifierHighMemory
  expr: process_resident_memory_bytes{job="classifier"} > 2 * 1024 * 1024 * 1024
```

`process_resident_memory_bytes` is an auto-generated metric from
`prometheus_client`'s default Python collector. It reads `/proc/self/status` for
the VmRSS (resident set size) — the actual physical RAM being used.

The INT8 model is 120 MB on disk, but ORT loads it into memory as a session
object and keeps inference buffers warm, expanding to roughly 300–500 MB. 2 GB
threshold gives ~4× headroom above normal operation. If it triggers, look for:
- Python objects holding references to large numpy arrays
- `asyncio.Task` leaks (tasks created but never awaited)
- Multiple model versions loaded simultaneously

```yaml
- alert: ClassifierFdLeak
  expr: process_open_fds{job="classifier"} > 200
  for: 5m
```

Normal steady-state fd count for the classifier is roughly:
- 3 standard streams (stdin, stdout, stderr)
- A few asyncpg pool connections (each DB connection = 1 fd)
- 1 ONNX model file
- Log file handlers

Well under 50. Hitting 200 means something is not closing fds — typically HTTP
connections or asyncpg pool connections that timed out but were not released.

**Tip:** run `ls -la /proc/$(pgrep -f uvicorn)/fd | wc -l` on the host to see
the live fd count without waiting for the next Prometheus scrape.

---

## Useful PromQL to run in Grafana Explore

```promql
# Live request rate split by label (harm vs safe)
sum(rate(classifier_requests_total[2m])) by (label)

# Latency heatmap (requires histogram)
rate(classifier_request_latency_seconds_bucket[5m])

# P99 latency over time
classifier:request_latency_p99:5m

# Batch size distribution at the 90th percentile
histogram_quantile(0.90, rate(classifier_batch_size_bucket[5m]))

# Error log rate by level
rate(classifier_log_errors_total[5m])

# Prometheus itself: how long scraping the classifier takes
scrape_duration_seconds{job="classifier"}
```

---

## Adding a new service to Prometheus

1. Have the service expose a `/metrics` endpoint (Prometheus exposition format).
2. Add a scrape job to `prometheus.yml`:
   ```yaml
   - job_name: stream-processor
     metrics_path: /metrics
     scrape_interval: 15s
     static_configs:
       - targets: ["host.k3d.internal:8001"]  # or cluster DNS if in K8s
     relabel_configs:
       - target_label: service
         replacement: stream-processor
   ```
3. Create `rules/stream-processor.yml` with alert and recording rules.
4. Add it to the `rule_files` list in `prometheus.yml`.
5. Reload Prometheus without restarting: `curl -X POST http://localhost:9090/-/reload`

The reload is hot — no scrape targets are missed, no time series are lost.
