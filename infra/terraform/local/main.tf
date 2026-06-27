# ── Namespaces ────────────────────────────────────────────────────────────────

locals {
  namespaces = [
    "sentinel-app",        # classifier, stream processor, data simulator
    "sentinel-data",       # PostgreSQL, MongoDB, MinIO, Kafka
    "sentinel-monitoring", # OTel Collector, Jaeger, Prometheus, Grafana
    "sentinel-pipeline",   # Airflow, Spark
  ]
}

resource "kubernetes_namespace" "sentinel" {
  for_each = toset(local.namespaces)

  metadata {
    name = each.key
    labels = {
      "managed-by" = "terraform"
    }
  }
}

# ── PostgreSQL ────────────────────────────────────────────────────────────────
# Uses the official postgres:16 image (Docker Hub, free) instead of Bitnami,
# which restricted image access in August 2025.
# Pattern: Secret → ConfigMap → StatefulSet → Service, each depends on the prior.

resource "kubernetes_secret" "postgres" {
  metadata {
    name      = "postgresql-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }
  data = {
    password = var.postgres_password
  }
}

resource "kubernetes_config_map" "postgres_init" {
  metadata {
    name      = "postgresql-init"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  data = {
    # The official postgres image runs every *.sql file in
    # /docker-entrypoint-initdb.d/ on first startup (empty data dir only).
    # Filenames are executed in lexicographic order — prefix with 01_, 02_ etc.
    "01_schema.sql" = <<-SQL
      CREATE TABLE IF NOT EXISTS model_registry (
          id            BIGSERIAL    PRIMARY KEY,
          model_version VARCHAR(100) NOT NULL UNIQUE,
          model_path    TEXT         NOT NULL,
          threshold     FLOAT        NOT NULL DEFAULT 0.5,
          status        VARCHAR(20)  NOT NULL
                        CHECK (status IN ('staging', 'active', 'retired')),
          created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
          promoted_at   TIMESTAMPTZ
      );

      CREATE TABLE IF NOT EXISTS classifications (
          id            BIGSERIAL    PRIMARY KEY,
          ts            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
          input_text    TEXT         NOT NULL,
          label         VARCHAR(10)  NOT NULL,
          score         FLOAT        NOT NULL,
          model_version VARCHAR(100) NOT NULL
                        REFERENCES model_registry(model_version),
          latency_ms    FLOAT        NOT NULL,
          inference_at  TIMESTAMPTZ  NOT NULL
      );

      CREATE INDEX IF NOT EXISTS classifications_ts_idx
          ON classifications (ts DESC);

      CREATE INDEX IF NOT EXISTS classifications_label_ts_idx
          ON classifications (label, ts DESC);

      CREATE INDEX IF NOT EXISTS classifications_model_version_ts_idx
          ON classifications (model_version, ts DESC);
    SQL
  }
}

resource "kubernetes_stateful_set" "postgresql" {
  metadata {
    name      = "postgresql"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
    labels    = { app = "postgresql" }
  }

  spec {
    service_name = "postgresql"
    replicas     = 1

    selector {
      match_labels = { app = "postgresql" }
    }

    template {
      metadata {
        labels = { app = "postgresql" }
      }

      spec {
        container {
          name  = "postgresql"
          image = "postgres:16"

          env {
            name  = "POSTGRES_USER"
            value = "sentinel"
          }
          env {
            name  = "POSTGRES_DB"
            value = "sentinel"
          }
          env {
            name = "POSTGRES_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.postgres.metadata[0].name
                key  = "password"
              }
            }
          }
          # sub_path prevents postgres complaining about a non-empty mount dir.
          # Without it, the volume root contains a lost+found dir on some
          # storage classes, which postgres treats as an initialised data dir.
          volume_mount {
            name       = "data"
            mount_path = "/var/lib/postgresql/data"
            sub_path   = "pgdata"
          }
          volume_mount {
            name       = "init"
            mount_path = "/docker-entrypoint-initdb.d"
          }

          port {
            container_port = 5432
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }

          readiness_probe {
            exec {
              command = ["pg_isready", "-U", "sentinel", "-d", "sentinel"]
            }
            initial_delay_seconds = 5
            period_seconds        = 5
            failure_threshold     = 6
          }
        }

        volume {
          name = "init"
          config_map {
            name = kubernetes_config_map.postgres_init.metadata[0].name
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "local-path"
        resources {
          requests = { storage = var.postgres_storage_size }
        }
      }
    }
  }

  # Block until the pod is Running and passing readiness probes before
  # Terraform reports success — same guarantee as helm_release wait = true.
  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "postgresql" {
  metadata {
    name      = "postgresql"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    selector = { app = "postgresql" }

    port {
      port        = 5432
      target_port = 5432
    }

    type = "ClusterIP"
  }
}

# ── MongoDB ───────────────────────────────────────────────────────────────────
# Stores flagged_content — harmful classifications used as training samples when
# Airflow triggers a retrain. Stream processor writes harmful spans here;
# PostgreSQL gets every classification for analytics.
# Uses official mongo:7 image (Docker Hub, free).

resource "kubernetes_secret" "mongodb" {
  metadata {
    name      = "mongodb-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }
  data = {
    root-password     = var.mongodb_root_password
    sentinel-password = var.mongodb_password
  }
}

resource "kubernetes_config_map" "mongodb_init" {
  metadata {
    name      = "mongodb-init"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  data = {
    # mongosh runs .js files in /docker-entrypoint-initdb.d/ on first startup.
    # process.env is available in mongosh — used to avoid hardcoding the password.
    "01_init.js" = <<-JS
      db = db.getSiblingDB('sentinel');

      db.createUser({
        user: 'sentinel',
        pwd: process.env.MONGO_SENTINEL_PASSWORD,
        roles: [{ role: 'readWrite', db: 'sentinel' }]
      });

      db.createCollection('flagged_content');
      db.flagged_content.createIndex({ ts: -1 });
      db.flagged_content.createIndex({ label: 1, ts: -1 });
      db.flagged_content.createIndex({ model_version: 1, ts: -1 });
    JS
  }
}

resource "kubernetes_stateful_set" "mongodb" {
  metadata {
    name      = "mongodb"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
    labels    = { app = "mongodb" }
  }

  spec {
    service_name = "mongodb"
    replicas     = 1

    selector {
      match_labels = { app = "mongodb" }
    }

    template {
      metadata {
        labels = { app = "mongodb" }
      }

      spec {
        container {
          name  = "mongodb"
          image = "mongo:7"

          env {
            name  = "MONGO_INITDB_ROOT_USERNAME"
            value = "root"
          }
          env {
            name = "MONGO_INITDB_ROOT_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.mongodb.metadata[0].name
                key  = "root-password"
              }
            }
          }
          # Passed to the init script so it can create the sentinel user without
          # hardcoding the password in the ConfigMap.
          env {
            name = "MONGO_SENTINEL_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.mongodb.metadata[0].name
                key  = "sentinel-password"
              }
            }
          }

          port {
            container_port = 27017
          }

          volume_mount {
            name       = "data"
            mount_path = "/data/db"
          }
          volume_mount {
            name       = "init"
            mount_path = "/docker-entrypoint-initdb.d"
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }

          readiness_probe {
            exec {
              command = ["mongosh", "--quiet", "--eval", "db.adminCommand('ping')"]
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            timeout_seconds       = 10
            failure_threshold     = 6
          }
        }

        volume {
          name = "init"
          config_map {
            name = kubernetes_config_map.mongodb_init.metadata[0].name
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "local-path"
        resources {
          requests = { storage = var.mongodb_storage_size }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "mongodb" {
  metadata {
    name      = "mongodb"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    selector = { app = "mongodb" }

    port {
      port        = 27017
      target_port = 27017
    }

    type = "ClusterIP"
  }
}

# ── MinIO ─────────────────────────────────────────────────────────────────────
# S3-compatible object storage for ONNX model artifacts.
# model_registry.model_path values are MinIO object keys, e.g.:
#   models/v1.0.0/model_quantized.onnx
# Classifier pods download the active model from MinIO on startup.
# Port 9000: S3-compatible API used by application code (boto3/aiobotocore).
# Port 9001: web console for inspecting buckets during local dev.
# Uses official minio/minio image (Docker Hub, free).
# To update the image: check minio/minio on Docker Hub for the latest RELEASE tag.

resource "kubernetes_secret" "minio" {
  metadata {
    name      = "minio-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }
  data = {
    root-user     = var.minio_root_user
    root-password = var.minio_root_password
  }
}

resource "kubernetes_stateful_set" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
    labels    = { app = "minio" }
  }

  spec {
    service_name = "minio"
    replicas     = 1

    selector {
      match_labels = { app = "minio" }
    }

    template {
      metadata {
        labels = { app = "minio" }
      }

      spec {
        container {
          name  = "minio"
          image = "minio/minio:RELEASE.2024-11-07T00-52-20Z"

          # "server /data" starts MinIO in single-node mode.
          # --console-address pins the console to port 9001 so it doesn't
          # pick a random port, making the Service port mapping deterministic.
          args = ["server", "/data", "--console-address", ":9001"]

          env {
            name = "MINIO_ROOT_USER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio.metadata[0].name
                key  = "root-user"
              }
            }
          }
          env {
            name = "MINIO_ROOT_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio.metadata[0].name
                key  = "root-password"
              }
            }
          }

          port {
            name           = "api"
            container_port = 9000
          }
          port {
            name           = "console"
            container_port = 9001
          }

          volume_mount {
            name       = "data"
            mount_path = "/data"
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }

          # /minio/health/ready returns 200 only when MinIO is accepting S3 requests.
          readiness_probe {
            http_get {
              path = "/minio/health/ready"
              port = 9000
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 6
          }

          # /minio/health/live returns 200 as long as the process is alive.
          liveness_probe {
            http_get {
              path = "/minio/health/live"
              port = 9000
            }
            initial_delay_seconds = 30
            period_seconds        = 20
            failure_threshold     = 3
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "local-path"
        resources {
          requests = { storage = var.minio_storage_size }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "minio" {
  metadata {
    name      = "minio"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    selector = { app = "minio" }

    port {
      name        = "api"
      port        = 9000
      target_port = 9000
    }

    port {
      name        = "console"
      port        = 9001
      target_port = 9001
    }

    type = "ClusterIP"
  }
}

# ── MinIO bucket init ──────────────────────────────────────────────────────────
# One-shot Job that creates the two buckets Sentinel needs.
# Runs after the StatefulSet is ready (depends_on + wait_for_rollout above).
# The until loop retries the alias-set command to handle the brief window
# between the readiness probe passing and the first external request succeeding.
# --ignore-existing makes mc mb idempotent — safe to re-apply.
#
# Buckets created:
#   models    — ONNX artifacts uploaded by the optimizer pipeline
#   datasets  — training data archives used by the retrain pipeline

resource "kubernetes_job_v1" "minio_init" {
  metadata {
    name      = "minio-bucket-init"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    template {
      metadata {}

      spec {
        restart_policy = "OnFailure"

        container {
          name  = "mc"
          image = "minio/mc:RELEASE.2024-11-21T17-21-54Z"

          command = [
            "/bin/sh", "-c",
            <<-SCRIPT
              until mc alias set minio http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" --api S3v4; do
                echo "MinIO not ready yet, retrying in 3s..."
                sleep 3
              done
              mc mb --ignore-existing minio/models
              mc mb --ignore-existing minio/datasets
              echo "Buckets ready."
            SCRIPT
          ]

          env {
            name = "MINIO_ROOT_USER"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio.metadata[0].name
                key  = "root-user"
              }
            }
          }
          env {
            name = "MINIO_ROOT_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.minio.metadata[0].name
                key  = "root-password"
              }
            }
          }
        }
      }
    }

    backoff_limit = 4
  }

  depends_on = [
    kubernetes_stateful_set.minio,
    kubernetes_service.minio,
  ]

  wait_for_completion = true

  timeouts {
    create = "3m"
  }
}

# ── Prometheus ────────────────────────────────────────────────────────────────
# Scrapes /metrics from the classifier every 10s and stores time-series in its
# local TSDB (7-day retention). Grafana reads from Prometheus as its datasource.
# Uses a StatefulSet for the PVC — TSDB data must survive pod restarts.
#
# ConfigMap carries two keys:
#   prometheus.yml        → /etc/prometheus/prometheus.yml
#   classifier-rules.yml  → /etc/prometheus/rules/classifier.yml
# sub_path mounts each key as a specific file so they land at different paths
# without clobbering the rest of the directory.

resource "kubernetes_config_map" "prometheus_config" {
  metadata {
    name      = "prometheus-config"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  data = {
    "prometheus.yml"       = file("${path.module}/../../prometheus/prometheus.yml")
    "classifier-rules.yml" = file("${path.module}/../../prometheus/rules/classifier.yml")
  }
}

resource "kubernetes_stateful_set" "prometheus" {
  metadata {
    name      = "prometheus"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
    labels    = { app = "prometheus" }
  }

  spec {
    service_name = "prometheus"
    replicas     = 1

    selector {
      match_labels = { app = "prometheus" }
    }

    template {
      metadata {
        labels = { app = "prometheus" }
      }

      spec {
        # Prometheus image runs as uid 65534 (nobody). fs_group ensures the PVC
        # is group-owned by 65534 so Prometheus can write to its TSDB.
        security_context {
          fs_group = 65534
        }

        container {
          name  = "prometheus"
          image = "prom/prometheus:v2.55.0"

          args = [
            "--config.file=/etc/prometheus/prometheus.yml",
            "--storage.tsdb.path=/prometheus",
            "--storage.tsdb.retention.time=7d",
            # Enables POST /-/reload to hot-reload config without pod restart.
            "--web.enable-lifecycle",
          ]

          port {
            container_port = 9090
          }

          # Mount prometheus.yml as a single file via sub_path so the rest of
          # /etc/prometheus/ is not replaced by the ConfigMap mount.
          volume_mount {
            name       = "config"
            mount_path = "/etc/prometheus/prometheus.yml"
            sub_path   = "prometheus.yml"
          }
          volume_mount {
            name       = "config"
            mount_path = "/etc/prometheus/rules/classifier.yml"
            sub_path   = "classifier-rules.yml"
          }
          volume_mount {
            name       = "data"
            mount_path = "/prometheus"
          }

          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }

          readiness_probe {
            http_get {
              path = "/-/ready"
              port = 9090
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/-/healthy"
              port = 9090
            }
            initial_delay_seconds = 30
            period_seconds        = 15
            failure_threshold     = 3
          }
        }

        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map.prometheus_config.metadata[0].name
          }
        }
      }
    }

    volume_claim_template {
      metadata {
        name = "data"
      }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "local-path"
        resources {
          requests = { storage = var.prometheus_storage_size }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "prometheus" {
  metadata {
    name      = "prometheus"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  spec {
    selector = { app = "prometheus" }

    port {
      port        = 9090
      target_port = 9090
    }

    type = "ClusterIP"
  }
}

# ── Grafana ───────────────────────────────────────────────────────────────────
# Visualisation layer on top of Prometheus. Uses a Deployment (not StatefulSet)
# because Grafana's state is fully reproduced from provisioning ConfigMaps —
# no PVC needed in local dev.
#
# Provisioning wires the Prometheus datasource automatically on first boot so
# you don't need to click through the UI to add it.

resource "kubernetes_secret" "grafana" {
  metadata {
    name      = "grafana-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }
  data = {
    admin-password = var.grafana_admin_password
  }
}

resource "kubernetes_config_map" "grafana_datasources" {
  metadata {
    name      = "grafana-datasources"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  data = {
    "prometheus.yml" = file("${path.module}/../../grafana/provisioning/datasources/prometheus.yml")
  }
}

resource "kubernetes_deployment" "grafana" {
  metadata {
    name      = "grafana"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
    labels    = { app = "grafana" }
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "grafana" }
    }

    template {
      metadata {
        labels = { app = "grafana" }
      }

      spec {
        # Grafana image runs as uid 472. fs_group makes any mounted volumes
        # group-writable by the grafana process.
        security_context {
          fs_group = 472
        }

        container {
          name  = "grafana"
          image = "grafana/grafana:11.3.0"

          env {
            name  = "GF_SECURITY_ADMIN_USER"
            value = "admin"
          }
          env {
            name = "GF_SECURITY_ADMIN_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.grafana.metadata[0].name
                key  = "admin-password"
              }
            }
          }
          # Disable phone-home / update checks in local dev.
          env {
            name  = "GF_ANALYTICS_REPORTING_ENABLED"
            value = "false"
          }
          env {
            name  = "GF_ANALYTICS_CHECK_FOR_UPDATES"
            value = "false"
          }

          port {
            container_port = 3000
          }

          volume_mount {
            name       = "datasources"
            mount_path = "/etc/grafana/provisioning/datasources"
          }

          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "200m", memory = "256Mi" }
          }

          readiness_probe {
            http_get {
              path = "/api/health"
              port = 3000
            }
            initial_delay_seconds = 15
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/api/health"
              port = 3000
            }
            initial_delay_seconds = 30
            period_seconds        = 15
            failure_threshold     = 3
          }
        }

        volume {
          name = "datasources"
          config_map {
            name = kubernetes_config_map.grafana_datasources.metadata[0].name
          }
        }
      }
    }
  }

  wait_for_rollout = true

  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "grafana" {
  metadata {
    name      = "grafana"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  spec {
    selector = { app = "grafana" }

    port {
      port        = 3000
      target_port = 3000
    }

    type = "ClusterIP"
  }
}

# ── mongo-express ──────────────────────────────────────────────────────────────
# Browser UI for inspecting MongoDB collections during local dev.
# Connects as the sentinel application user (read-write on the sentinel database).
# Basic auth disabled — only port-forwarded to localhost, never internet-exposed.
# Useful for inspecting flagged_content as the stream processor (Phase 5) writes
# harmful spans, and for verifying document shape before building the retrain DAG.

resource "kubernetes_deployment" "mongo_express" {
  metadata {
    name      = "mongo-express"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
    labels    = { app = "mongo-express" }
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "mongo-express" }
    }

    template {
      metadata {
        labels = { app = "mongo-express" }
      }

      spec {
        container {
          name  = "mongo-express"
          image = "mongo-express:1.0.2-20"

          # The entrypoint only reads ME_CONFIG_MONGODB_URL — individual SERVER/PORT
          # vars are not used for the startup connectivity check. K8s expands
          # $(VAR_NAME) in env values to inject the secret password into the URL.
          # Root user is required: mongo-express runs db.adminCommand(serverStatus)
          # on startup, which the sentinel app user (readWrite on sentinel db only)
          # is not authorized to execute. Local dev only — never internet-exposed.
          env {
            name = "ME_CONFIG_MONGODB_ROOT_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.mongodb.metadata[0].name
                key  = "root-password"
              }
            }
          }
          env {
            name  = "ME_CONFIG_MONGODB_URL"
            value = "mongodb://root:$(ME_CONFIG_MONGODB_ROOT_PASSWORD)@mongodb:27017/?authSource=admin"
          }
          env {
            name  = "ME_CONFIG_BASICAUTH"
            value = "false"
          }

          port {
            container_port = 8081
          }

          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "100m", memory = "128Mi" }
          }

          readiness_probe {
            http_get {
              path = "/"
              port = 8081
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/"
              port = 8081
            }
            initial_delay_seconds = 20
            period_seconds        = 15
            failure_threshold     = 3
          }
        }
      }
    }
  }

  depends_on = [kubernetes_stateful_set.mongodb, kubernetes_service.mongodb]

  wait_for_rollout = true

  timeouts {
    create = "3m"
    update = "3m"
  }
}

resource "kubernetes_service" "mongo_express" {
  metadata {
    name      = "mongo-express"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    selector = { app = "mongo-express" }

    port {
      port        = 8081
      target_port = 8081
    }

    type = "ClusterIP"
  }
}
