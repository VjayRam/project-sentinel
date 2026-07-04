# ── MLflow (Phase 7: experiment tracking) ─────────────────────────────────────
# Deployment, not StatefulSet — all state lives in Postgres (backend store:
# experiments/runs/params/metrics) and MinIO (artifact store: model files),
# same reasoning as Grafana. No PVC needed.
#
# Custom image (infra/mlflow/Dockerfile) — the official ghcr.io/mlflow/mlflow
# image doesn't bundle a Postgres driver or S3 client, so `mlflow server
# --backend-store-uri postgresql://...` and `--default-artifact-root s3://...`
# both fail to start against the base image. Built and imported into k3d by
# dev-start.sh, same as classifier/stream-processor/drift.
#
# Backend DB and MinIO bucket: see main.tf's postgres_init (03_mlflow_db.sql)
# and minio_init Job (mc mb minio/mlflow) — reuses existing infra rather than
# standing up a second Postgres/object store instance.

resource "kubernetes_secret" "monitoring_postgres" {
  metadata {
    name      = "postgresql-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }
  data = {
    password = var.postgres_password
  }
}

resource "kubernetes_secret" "monitoring_minio" {
  metadata {
    name      = "minio-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }
  data = {
    root-user     = var.minio_root_user
    root-password = var.minio_root_password
  }
}

resource "kubernetes_deployment" "mlflow" {
  metadata {
    name      = "mlflow"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
    labels    = { app = "mlflow" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "mlflow" } }

    template {
      metadata { labels = { app = "mlflow" } }

      spec {
        container {
          name              = "mlflow"
          image             = "sentinel-mlflow:local"
          image_pull_policy = "Never"

          env {
            name = "PG_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.monitoring_postgres.metadata[0].name
                key  = "password"
              }
            }
          }
          # --serve-artifacts: proxies artifact reads/writes through the
          # tracking server itself rather than handing clients direct MinIO
          # credentials — keeps AWS_ACCESS_KEY_ID/SECRET scoped to this pod.
          command = ["mlflow"]
          args = [
            "server",
            "--backend-store-uri", "postgresql://sentinel:$(PG_PASSWORD)@postgresql.sentinel-data.svc.cluster.local:5432/mlflow",
            "--default-artifact-root", "s3://mlflow/",
            "--serve-artifacts",
            "--host", "0.0.0.0",
            "--port", "5000",
            # Default is 4 workers, but each one is heavy enough that 4 of
            # them starting concurrently OOM-killed the pod even at a 1Gi
            # limit (live-reproduced) — no concurrent load in local dev to
            # justify the default worker count anyway.
            "--workers", "2",
            # Default allowed-hosts is localhost + private IPs only — the
            # retraining pod connects via the in-cluster DNS name, not a raw
            # IP, and got rejected with a 403 "possible DNS rebinding
            # attack" (live-reproduced) since that name isn't in the
            # default list. --allowed-hosts replaces the default rather
            # than extending it, so localhost has to be re-added explicitly
            # too, or the port-forwarded UI/browser access breaks instead.
            # Matching is against the full Host header including port
            # (bare hostnames alone still 403'd, live-reproduced) — both
            # forms are listed since kubectl port-forward's client sends
            # "localhost:5000" while some tools may send bare "localhost".
            "--allowed-hosts", join(",", [
              "localhost", "localhost:5000",
              "mlflow", "mlflow:5000",
              "mlflow.sentinel-monitoring.svc.cluster.local", "mlflow.sentinel-monitoring.svc.cluster.local:5000",
              "mlflow.sentinel-monitoring.svc", "mlflow.sentinel-monitoring.svc:5000",
              "mlflow.sentinel-monitoring", "mlflow.sentinel-monitoring:5000",
            ]),
          ]

          env {
            name  = "MLFLOW_S3_ENDPOINT_URL"
            value = "http://minio.sentinel-data.svc.cluster.local:9000"
          }
          env {
            name = "AWS_ACCESS_KEY_ID"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.monitoring_minio.metadata[0].name
                key  = "root-user"
              }
            }
          }
          env {
            name = "AWS_SECRET_ACCESS_KEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.monitoring_minio.metadata[0].name
                key  = "root-password"
              }
            }
          }

          port { container_port = 5000 }

          # 512Mi and then 1Gi both OOM-killed the pod during startup
          # (live-reproduced twice) — the FastAPI/uvicorn tracking server's
          # default 4 worker processes (capped to 2 above) are heavier than
          # the single-process Grafana/Jaeger images this block was
          # originally copied from. Node has ample headroom (~10Gi free) —
          # this is purely a cgroup limit, not real resource contention.
          resources {
            requests = { cpu = "200m", memory = "768Mi" }
            limits   = { cpu = "1000m", memory = "2Gi" }
          }

          readiness_probe {
            http_get {
              path = "/health"
              port = 5000
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/health"
              port = 5000
            }
            initial_delay_seconds = 30
            period_seconds        = 15
            failure_threshold     = 3
          }
        }
      }
    }
  }

  # Same reasoning as the classifier: on the very first apply, the local
  # image may not exist yet (dev-start.sh builds it before this resource is
  # created, but a bare `terraform apply` without dev-start.sh would not).
  wait_for_rollout = false

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "mlflow" {
  metadata {
    name      = "mlflow"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  spec {
    selector = { app = "mlflow" }

    port {
      port        = 5000
      target_port = 5000
    }

    type = "ClusterIP"
  }
}
