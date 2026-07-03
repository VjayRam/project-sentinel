# Infra — Overview

The `infra/` directory contains everything needed to run Sentinel's supporting
infrastructure. All services run inside a local k3d Kubernetes cluster managed
entirely by Terraform — no `docker-compose.yml`, no manual `kubectl apply`.

---

## Directory structure

```
infra/
  terraform/
    local/                    — Terraform workspace for the k3d dev cluster
      providers.tf            — kubernetes + helm provider config
      variables.tf            — all tuneable knobs (passwords, sizes)
      main.tf                 — every K8s resource (namespaces → all services)
      outputs.tf              — port-forward commands printed after apply
  prometheus/
    prometheus.yml            — global config, scrape jobs, rule file references
    rules/
      classifier.yml          — alert + recording rules for the classifier
  grafana/
    provisioning/
      datasources/
        prometheus.yml        — auto-provision Prometheus as Grafana's datasource
```

---

## Component explanations

Each sub-directory has its own detailed explanation file:

- [`terraform/local/explanation.md`](terraform/local/explanation.md) — all Terraform
  resources: namespaces, PostgreSQL, MongoDB, MinIO, Prometheus, Grafana, Kafka,
  Jaeger, OTel Collector. Covers every provider block, variable, resource type,
  and the patterns used throughout (StatefulSet vs Deployment, wait_for_rollout,
  lifecycle hooks, etc.).

- [`prometheus/explanation.md`](prometheus/explanation.md) — global Prometheus
  config, the k3d host scraping trick, scrape job relabeling, recording rules,
  alert thresholds, and how to add new services to monitoring.

- [`grafana/explanation.md`](grafana/explanation.md) — Grafana provisioning system,
  datasource proxy model, editable vs managed resources, and how to build and
  persist dashboards.

---

## How the pieces connect

```
dev-start.sh
  → k3d cluster create/start sentinel
  → terraform apply (deploys everything below)

K8s cluster (sentinel-data namespace)
  PostgreSQL  :5432   — classification results, model registry
  MongoDB     :27017  — flagged content for retraining
  MinIO       :9000   — ONNX model artifacts
  Kafka       :9092   — traces.raw topic (3 partitions)

K8s cluster (sentinel-monitoring namespace)
  Prometheus  :9090   — scrapes classifier at host.k3d.internal:8000
  Grafana     :3000   — queries Prometheus via in-cluster DNS
  Jaeger      :16686  — receives OTLP traces from OTel Collector
  OTel Collector :4317/:4318  — receives spans, fans out to Kafka + Jaeger

Host machine (started by dev-start.sh, not in K8s)
  classifier  :8000   — FastAPI + ONNX inference
  stream-processor    — Kafka consumer → classify → PG + Mongo
```

Port-forwards from the cluster to localhost are opened automatically by
`dev-start.sh`. Each can also be opened individually — see each service's
section in `docs/local-dev.md`.
