# Infra — Overview

The `infra/` directory contains everything needed to run Sentinel's supporting
infrastructure. All services — including the classifier and stream processor
themselves, as of Phase 5 — run inside a local k3d Kubernetes cluster managed
entirely by Terraform. No `kubectl apply` files exist outside of what
Terraform generates; `docker-compose.yml` at the repo root is a secondary,
lightweight alternative for running just the classifier + Prometheus + Grafana
without a full cluster (see its own comments for the tradeoffs).

---

## Directory structure

```
infra/
  terraform/
    local/                    — Terraform workspace for the k3d dev cluster
      providers.tf            — kubernetes + helm provider config
      variables.tf            — all tuneable knobs (passwords, sizes, keys)
      main.tf                 — data/monitoring/app layer resources
      airflow.tf              — Airflow (Phase 7 orchestration)
      outputs.tf              — port-forward commands printed after apply
  prometheus/
    prometheus.yml            — k8s-path scrape config (rule_files: rules/)
    prometheus.compose.yml    — docker-compose-path scrape config (different
                                 classifier target — see its own comment)
    rules/
      classifier.yml          — alert + recording rules for the classifier
  grafana/
    provisioning/
      datasources/
        prometheus.yml        — auto-provision Prometheus as Grafana's datasource
```

---

## Component explanations

Each sub-directory (and several outside `infra/`) has its own detailed
explanation file:

- [`terraform/local/explanation.md`](terraform/local/explanation.md) — every
  Terraform resource: namespaces, PostgreSQL, MongoDB, MinIO, Prometheus,
  Grafana, Kafka, Jaeger, OTel Collector, spark-operator, and Airflow. Covers
  every provider block, variable, resource type, and the patterns used
  throughout (StatefulSet vs Deployment, `wait_for_rollout`, ConfigMap-as-file
  mounting, etc.) — plus a growing list of live-debugged gotchas.

- [`prometheus/explanation.md`](prometheus/explanation.md) — global Prometheus
  config, scrape job relabeling, recording rules, alert thresholds, and how to
  add new services to monitoring.

- [`grafana/explanation.md`](grafana/explanation.md) — Grafana provisioning
  system, datasource proxy model, editable vs managed resources, and how to
  build and persist dashboards.

- [`../pipelines/drift/explanation.md`](../pipelines/drift/explanation.md) —
  PySpark-based drift detection (PSI/JSD), the SparkApplication CRD, and how
  it's scheduled to run.

- [`../pipelines/evaluation/explanation.md`](../pipelines/evaluation/explanation.md)
  — the model quality gate: benchmark metrics, the ground-truth dataset, and
  what "passing" actually means before a model can be promoted.

- [`../orchestration/explanation.md`](../orchestration/explanation.md) — how
  Airflow DAGs get into the cluster and the CLI commands used to inspect them
  day to day.

---

## How the pieces connect

```
dev-start.sh
  → k3d cluster create/start sentinel
  → docker build + k3d image import (classifier, stream-processor, drift)
  → terraform apply (deploys everything below)

K8s cluster (sentinel-data namespace)
  PostgreSQL  :5432   — classification results, model registry, drift stats,
                        Airflow's own metadata (separate database)
  MongoDB     :27017  — flagged content for retraining
  MinIO       :9000   — ONNX model artifacts
  Kafka       :9092   — traces.raw topic (3 partitions)

K8s cluster (sentinel-app namespace)
  classifier         — FastAPI + ONNX inference, /v1/moderations primary endpoint
  stream-processor   — Kafka consumer → classify → PG + Mongo

K8s cluster (sentinel-monitoring namespace)
  Prometheus  :9090   — scrapes classifier via in-cluster Service DNS
  Grafana     :3000   — queries Prometheus via in-cluster DNS
  Jaeger      :16686  — receives OTLP traces from OTel Collector
  OTel Collector :4317/:4318  — receives spans, fans out to Kafka + Jaeger

K8s cluster (sentinel-pipeline namespace)
  spark-operator      — manages the drift job's driver/executor pods
  Airflow             — scheduler + webserver (LocalExecutor), orchestrates
                        pipelines/ jobs on a schedule (Phase 7)
```

Everything now runs in-cluster — there is no "host machine" component left.
Port-forwards from the cluster to localhost are opened automatically by
`dev-start.sh` for local access (curl, browser UIs, psql, etc.); each can also
be opened individually via the `terraform output` commands in `outputs.tf`.
See each service's section in `docs/local-dev.md` for URLs and credentials.
