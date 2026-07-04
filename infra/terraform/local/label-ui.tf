# ── Label UI (Phase 7: manual labelling for retraining) ──────────────────────
# Deployment in sentinel-app, same shape as classifier/stream-processor.
# Reads/writes MongoDB flagged_content directly (reuses the app_mongodb
# secret already mirrored into this namespace) and triggers orchestration/
# retrain_dag.py via Airflow's REST API (needs its own admin-password
# secret mirrored here — airflow_admin_password isn't otherwise available
# outside sentinel-pipeline, K8s secrets being namespace-scoped).

resource "kubernetes_secret" "app_airflow" {
  metadata {
    name      = "airflow-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  data = {
    admin-password = var.airflow_admin_password
  }
}

resource "kubernetes_deployment" "label_ui" {
  metadata {
    name      = "label-ui"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
    labels    = { app = "label-ui" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "label-ui" } }

    template {
      metadata { labels = { app = "label-ui" } }

      spec {
        container {
          name              = "label-ui"
          image             = "sentinel-label-ui:local"
          image_pull_policy = "Never"

          env {
            name = "MONGO_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_mongodb.metadata[0].name
                key  = "sentinel-password"
              }
            }
          }
          env {
            name  = "MONGO_URI"
            value = "mongodb://sentinel:$(MONGO_PASSWORD)@mongodb.sentinel-data.svc.cluster.local:27017/sentinel"
          }
          env {
            name  = "AIRFLOW_BASE_URL"
            value = "http://airflow-webserver.sentinel-pipeline.svc.cluster.local:8080"
          }
          env {
            name  = "AIRFLOW_ADMIN_USER"
            value = "admin"
          }
          env {
            name = "AIRFLOW_ADMIN_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_airflow.metadata[0].name
                key  = "admin-password"
              }
            }
          }

          port { container_port = 8001 }

          liveness_probe {
            http_get {
              path = "/health"
              port = 8001
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            failure_threshold     = 3
          }

          readiness_probe {
            http_get {
              path = "/health"
              port = 8001
            }
            initial_delay_seconds = 5
            period_seconds        = 5
            failure_threshold     = 6
          }

          resources {
            requests = { cpu = "50m", memory = "128Mi" }
            limits   = { cpu = "200m", memory = "256Mi" }
          }
        }
      }
    }
  }

  # Same reasoning as classifier/stream-processor: on the very first apply
  # the local image may not exist yet if this is applied outside dev-start.sh.
  wait_for_rollout = false

  timeouts {
    create = "3m"
    update = "3m"
  }
}

resource "kubernetes_service" "label_ui" {
  metadata {
    name      = "label-ui"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }

  spec {
    selector = { app = "label-ui" }
    port {
      port        = 8001
      target_port = 8001
    }
    type = "ClusterIP"
  }
}
