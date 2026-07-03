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
          inference_at  TIMESTAMPTZ  NOT NULL,
          span_id       TEXT,
          text_type     VARCHAR(8)
      );

      CREATE INDEX IF NOT EXISTS classifications_ts_idx
          ON classifications (ts DESC);

      CREATE INDEX IF NOT EXISTS classifications_label_ts_idx
          ON classifications (label, ts DESC);

      CREATE INDEX IF NOT EXISTS classifications_model_version_ts_idx
          ON classifications (model_version, ts DESC);

      -- Partial unique index for stream processor idempotency.
      -- ON CONFLICT (span_id, text_type) WHERE span_id IS NOT NULL DO NOTHING
      -- prevents duplicate rows when Kafka redelivers a message.
      CREATE UNIQUE INDEX IF NOT EXISTS classifications_span_id_text_type_idx
          ON classifications (span_id, text_type)
          WHERE span_id IS NOT NULL;

      CREATE TABLE IF NOT EXISTS drift_stats (
          id             BIGSERIAL    PRIMARY KEY,
          computed_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
          model_version  VARCHAR(100) NOT NULL,
          window_start   TIMESTAMPTZ  NOT NULL,
          window_end     TIMESTAMPTZ  NOT NULL,
          n_samples      INT          NOT NULL,
          psi            FLOAT        NOT NULL,
          jsd            FLOAT        NOT NULL,
          drift_flagged  BOOLEAN      NOT NULL
      );

      CREATE INDEX IF NOT EXISTS drift_stats_computed_at_idx
          ON drift_stats (computed_at DESC);

      CREATE INDEX IF NOT EXISTS drift_stats_model_version_idx
          ON drift_stats (model_version, computed_at DESC);
    SQL

    # Airflow's own metadata store — a separate database on the same
    # instance rather than a second PostgreSQL deployment (Phase 7 reuses
    # existing infra where it can, per the project's "don't add
    # infrastructure beyond what's needed" principle). CREATE DATABASE
    # can't run inside the same transaction/file as CREATE TABLE reliably
    # across all psql invocations, so it's a separate init file — this
    # image's entrypoint runs *.sql files individually, one psql
    # invocation per file, so a top-level CREATE DATABASE here is safe.
    "02_airflow_db.sql" = <<-SQL
      CREATE DATABASE airflow OWNER sentinel;
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

      // Mirrors the partial unique index on classifications (span_id, text_type)
      // in Postgres — enforces idempotency at the DB level so Kafka redelivery
      // can't duplicate a flagged_content doc, even if application code regresses.
      db.flagged_content.createIndex(
        { span_id: 1, text_type: 1 },
        { unique: true, partialFilterExpression: { span_id: { '$type': 'string' } } }
      );
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

# ── Kafka (KRaft, single broker) ──────────────────────────────────────────────
# Single combined broker+controller node — no ZooKeeper.
# Two listeners:
#   PLAINTEXT :9092  — in-cluster (OTel Collector → Kafka)
#   EXTERNAL  :9094  — host access via kubectl port-forward (stream processor)
# ADVERTISED_LISTENERS for EXTERNAL is localhost:9094 so metadata responses
# point back through the port-forward after the initial connection.

resource "kubernetes_stateful_set" "kafka" {
  metadata {
    name      = "kafka"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
    labels    = { app = "kafka" }
  }

  spec {
    service_name = "kafka"
    replicas     = 1

    selector {
      match_labels = { app = "kafka" }
    }

    template {
      metadata {
        labels = { app = "kafka" }
      }

      spec {
        container {
          name = "kafka"
          # Pinned to match whatever :latest had already drifted to and
          # formatted the PVC's KRaft metadata with (verified: the cached
          # apache/kafka:latest image is kafka_2.13-4.3.1.jar). KRaft's
          # on-disk metadata format is forward-compatible only — pinning to
          # an OLDER version than what already formatted the volume crashes
          # every broker start with "No MetadataVersion with feature level
          # N". If this version is ever bumped, wipe the PVC first
          # (kubectl delete pvc data-kafka-0 -n sentinel-data) or pin to a
          # version >= whatever last wrote to it.
          image = "apache/kafka:4.3.1"

          env {
            name  = "KAFKA_NODE_ID"
            value = "1"
          }
          env {
            name  = "KAFKA_PROCESS_ROLES"
            value = "broker,controller"
          }
          env {
            # apache/kafka's default log.dirs is /tmp/kraft-combined-logs, which is
            # NOT under the mounted PVC (see volume_mount below) — without this,
            # topic data silently doesn't survive pod restarts/reschedules.
            name  = "KAFKA_LOG_DIRS"
            value = "/bitnami/kafka/data"
          }
          env {
            name  = "KAFKA_CONTROLLER_QUORUM_VOTERS"
            value = "1@localhost:9093"
          }
          env {
            name  = "KAFKA_LISTENERS"
            value = "PLAINTEXT://:9092,CONTROLLER://:9093,EXTERNAL://:9094"
          }
          env {
            name  = "KAFKA_ADVERTISED_LISTENERS"
            value = "PLAINTEXT://kafka.sentinel-data.svc.cluster.local:9092,EXTERNAL://localhost:9094"
          }
          env {
            name  = "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP"
            value = "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT"
          }
          env {
            name  = "KAFKA_CONTROLLER_LISTENER_NAMES"
            value = "CONTROLLER"
          }
          env {
            name  = "KAFKA_INTER_BROKER_LISTENER_NAME"
            value = "PLAINTEXT"
          }
          env {
            name  = "KAFKA_AUTO_CREATE_TOPICS_ENABLE"
            value = "false"
          }
          env {
            name  = "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR"
            value = "1"
          }
          env {
            name  = "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR"
            value = "1"
          }
          env {
            name  = "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR"
            value = "1"
          }

          port {
            container_port = 9092
            name           = "plaintext"
          }
          port {
            container_port = 9093
            name           = "controller"
          }
          port {
            container_port = 9094
            name           = "external"
          }

          resources {
            requests = { cpu = "200m", memory = "512Mi" }
            limits   = { cpu = "500m", memory = "1Gi" }
          }

          readiness_probe {
            tcp_socket { port = 9092 }
            initial_delay_seconds = 30
            period_seconds        = 10
            failure_threshold     = 6
          }

          liveness_probe {
            tcp_socket { port = 9092 }
            initial_delay_seconds = 60
            period_seconds        = 30
            failure_threshold     = 3
          }

          volume_mount {
            name       = "data"
            mount_path = "/bitnami/kafka"
          }
        }
      }
    }

    volume_claim_template {
      metadata { name = "data" }
      spec {
        access_modes       = ["ReadWriteOnce"]
        storage_class_name = "local-path"
        resources { requests = { storage = var.kafka_storage_size } }
      }
    }
  }

  wait_for_rollout = true
  timeouts {
    create = "5m"
    update = "5m"
  }
}

resource "kubernetes_service" "kafka" {
  metadata {
    name      = "kafka"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    selector = { app = "kafka" }
    port {
      name        = "plaintext"
      port        = 9092
      target_port = 9092
    }
    port {
      name        = "external"
      port        = 9094
      target_port = 9094
    }
    type = "ClusterIP"
  }
}

resource "kubernetes_job_v1" "kafka_topic_init" {
  metadata {
    name      = "kafka-topic-init"
    namespace = kubernetes_namespace.sentinel["sentinel-data"].metadata[0].name
  }

  spec {
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        container {
          name = "kafka-topic-init"
          # Matches the broker's pinned version above — this only runs
          # kafka-topics.sh as a client, but keeping both pins identical
          # avoids a second version to track.
          image = "apache/kafka:4.3.1"
          command = [
            "/bin/sh", "-c",
            "/opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --if-not-exists --topic traces.raw --partitions 3 --replication-factor 1 && echo 'topic ready'"
          ]
          resources {
            requests = { cpu = "50m", memory = "128Mi" }
            limits   = { cpu = "100m", memory = "256Mi" }
          }
        }
      }
    }
    backoff_limit = 10
  }

  depends_on          = [kubernetes_stateful_set.kafka, kubernetes_service.kafka]
  wait_for_completion = true
  timeouts { create = "5m" }
}

# ── Jaeger (all-in-one, in-memory) ────────────────────────────────────────────
# Local dev only — traces do not survive pod restarts.
# OTLP gRPC receiver on :4317 (used by OTel Collector).
# UI on :16686.

resource "kubernetes_deployment" "jaeger" {
  metadata {
    name      = "jaeger"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
    labels    = { app = "jaeger" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "jaeger" } }

    template {
      metadata { labels = { app = "jaeger" } }

      spec {
        container {
          name  = "jaeger"
          image = "jaegertracing/all-in-one:1.62.0"

          env {
            name  = "COLLECTOR_OTLP_ENABLED"
            value = "true"
          }

          port {
            container_port = 16686
            name           = "ui"
          }
          port {
            container_port = 4317
            name           = "otlp-grpc"
          }

          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "200m", memory = "256Mi" }
          }

          readiness_probe {
            http_get {
              path = "/"
              port = 16686
            }
            initial_delay_seconds = 5
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/"
              port = 16686
            }
            initial_delay_seconds = 15
            period_seconds        = 15
            failure_threshold     = 3
          }
        }
      }
    }
  }

  wait_for_rollout = true
  timeouts {
    create = "3m"
    update = "3m"
  }
}

resource "kubernetes_service" "jaeger" {
  metadata {
    name      = "jaeger"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  spec {
    selector = { app = "jaeger" }
    port {
      name        = "ui"
      port        = 16686
      target_port = 16686
    }
    port {
      name        = "otlp-grpc"
      port        = 4317
      target_port = 4317
    }
    type = "ClusterIP"
  }
}

# ── OTel Collector ────────────────────────────────────────────────────────────
# Receives OTLP traces from chat apps (gRPC :4317, HTTP :4318).
# Fan-out: Kafka exporter → traces.raw topic + OTLP exporter → Jaeger.
# Uses the contrib image for the Kafka exporter.

resource "kubernetes_config_map" "otel_collector_config" {
  metadata {
    name      = "otel-collector-config"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  data = {
    "config.yaml" = <<-YAML
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318

    processors:
      batch:
        timeout: 1s
        send_batch_size: 100

    exporters:
      kafka:
        brokers:
          - kafka.sentinel-data.svc.cluster.local:9092
        topic: traces.raw
        encoding: otlp_json
      otlp/jaeger:
        endpoint: jaeger.sentinel-monitoring.svc.cluster.local:4317
        tls:
          insecure: true

    extensions:
      health_check:
        endpoint: 0.0.0.0:13133

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

resource "kubernetes_deployment" "otel_collector" {
  metadata {
    name      = "otel-collector"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
    labels    = { app = "otel-collector" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "otel-collector" } }

    template {
      metadata { labels = { app = "otel-collector" } }

      spec {
        container {
          name  = "otel-collector"
          image = "otel/opentelemetry-collector-contrib:0.113.0"

          args = ["--config=/etc/otelcol/config.yaml"]

          port {
            container_port = 4317
            name           = "otlp-grpc"
          }
          port {
            container_port = 4318
            name           = "otlp-http"
          }
          port {
            container_port = 13133
            name           = "health"
          }

          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "200m", memory = "256Mi" }
          }

          readiness_probe {
            http_get {
              path = "/"
              port = 13133
            }
            initial_delay_seconds = 10
            period_seconds        = 5
            failure_threshold     = 6
          }

          liveness_probe {
            http_get {
              path = "/"
              port = 13133
            }
            initial_delay_seconds = 20
            period_seconds        = 15
            failure_threshold     = 3
          }

          volume_mount {
            name       = "config"
            mount_path = "/etc/otelcol"
          }
        }

        volume {
          name = "config"
          config_map {
            name = kubernetes_config_map.otel_collector_config.metadata[0].name
          }
        }
      }
    }
  }

  depends_on = [
    kubernetes_job_v1.kafka_topic_init,
    kubernetes_deployment.jaeger,
    kubernetes_service.jaeger,
  ]

  wait_for_rollout = true
  timeouts {
    create = "3m"
    update = "3m"
  }
}

resource "kubernetes_service" "otel_collector" {
  metadata {
    name      = "otel-collector"
    namespace = kubernetes_namespace.sentinel["sentinel-monitoring"].metadata[0].name
  }

  spec {
    selector = { app = "otel-collector" }
    port {
      name        = "otlp-grpc"
      port        = 4317
      target_port = 4317
    }
    port {
      name        = "otlp-http"
      port        = 4318
      target_port = 4318
    }
    type = "ClusterIP"
  }
}

# ── App-layer secrets (sentinel-app) ─────────────────────────────────────────
# K8s secrets are namespace-scoped. The data-layer secrets live in sentinel-data
# and are unreachable from sentinel-app pods. Mirror the needed credentials here
# using the same variable values — single source of truth stays in variables.tf.

resource "kubernetes_secret" "app_postgres" {
  metadata {
    name      = "postgresql-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  data = {
    password = var.postgres_password
  }
}

resource "kubernetes_secret" "app_minio" {
  metadata {
    name      = "minio-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  data = {
    root-user     = var.minio_root_user
    root-password = var.minio_root_password
  }
}

resource "kubernetes_secret" "app_mongodb" {
  metadata {
    name      = "mongodb-credentials"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  data = {
    sentinel-password = var.mongodb_password
  }
}

# ── Classifier (sentinel-app) ─────────────────────────────────────────────────
# Image is built locally and imported into k3d by dev-start.sh before apply.
# imagePullPolicy=Never tells K8s to use the local image store only — no registry.
# wait_for_rollout=false because on the very first apply the image may not exist
# yet (dev-start.sh imports it then does a rollout restart).
# Env vars use K8s $(VAR) substitution to inject secret values into DSN strings.

resource "kubernetes_deployment" "classifier" {
  metadata {
    name      = "classifier"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
    labels    = { app = "classifier" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "classifier" } }

    template {
      metadata { labels = { app = "classifier" } }

      spec {
        container {
          name              = "classifier"
          image             = "sentinel-classifier:local"
          image_pull_policy = "Never"

          env {
            name = "PG_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_postgres.metadata[0].name
                key  = "password"
              }
            }
          }
          env {
            name  = "DATABASE_URL"
            value = "postgresql://sentinel:$(PG_PASSWORD)@postgresql.sentinel-data.svc.cluster.local:5432/sentinel"
          }
          env {
            name  = "MINIO_ENDPOINT"
            value = "http://minio.sentinel-data.svc.cluster.local:9000"
          }
          env {
            name = "MINIO_ACCESS_KEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_minio.metadata[0].name
                key  = "root-user"
              }
            }
          }
          env {
            name = "MINIO_SECRET_KEY"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_minio.metadata[0].name
                key  = "root-password"
              }
            }
          }

          port { container_port = 8000 }

          liveness_probe {
            http_get {
              path = "/health/live"
              port = 8000
            }
            initial_delay_seconds = 5
            period_seconds        = 10
            failure_threshold     = 3
          }

          readiness_probe {
            http_get {
              path = "/health/ready"
              port = 8000
            }
            initial_delay_seconds = 15
            period_seconds        = 5
            failure_threshold     = 6
          }

          resources {
            requests = { cpu = "200m", memory = "256Mi" }
            limits   = { cpu = "1000m", memory = "1Gi" }
          }
        }
      }
    }
  }

  wait_for_rollout = false

  timeouts {
    create = "5m"
    update = "5m"
  }
}

# ── Phase 6: Spark on Kubernetes ──────────────────────────────────────────────

# spark-on-k8s-operator watches sentinel-pipeline for SparkApplication CRDs
# and submits them to the cluster as driver + executor pods.
resource "helm_release" "spark_operator" {
  name             = "spark-operator"
  repository       = "https://kubeflow.github.io/spark-operator"
  chart            = "spark-operator"
  version          = "~2.1"
  namespace        = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  create_namespace = false
  wait             = true

  values = [yamlencode({
    controller = {
      workers = 1
    }
    webhook = {
      enable = true
    }
    spark = {
      jobNamespaces = ["sentinel-pipeline"]
    }
  })]
}

# ServiceAccount the driver pod runs as — must have permission to create/delete
# executor pods within sentinel-pipeline.
resource "kubernetes_service_account" "spark" {
  metadata {
    name      = "spark"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

resource "kubernetes_role" "spark_driver" {
  metadata {
    name      = "spark-driver"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods", "services", "configmaps", "persistentvolumeclaims"]
    verbs      = ["create", "get", "list", "watch", "delete", "deletecollection", "update", "patch"]
  }
}

resource "kubernetes_role_binding" "spark_driver" {
  metadata {
    name      = "spark-driver"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.spark_driver.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.spark.metadata[0].name
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

# PostgreSQL DSN for the drift driver pod — identical pattern to app_postgres
# but in sentinel-pipeline namespace (secrets are namespace-scoped).
resource "kubernetes_secret" "drift_postgres" {
  metadata {
    name      = "drift-postgres"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  data = {
    database-url = "postgresql://sentinel:${var.postgres_password}@postgresql.sentinel-data.svc.cluster.local:5432/sentinel"
  }
}

resource "kubernetes_service" "classifier" {
  metadata {
    name      = "classifier"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }

  spec {
    selector = { app = "classifier" }
    port {
      port        = 8000
      target_port = 8000
    }
    type = "ClusterIP"
  }
}

# ── Stream Processor (sentinel-app) ───────────────────────────────────────────
# Consumer only — no Service needed (nothing calls into it).
# Connects to Kafka via PLAINTEXT in-cluster listener (no port-forward needed).
# Connects to classifier via in-cluster DNS — no port-forward needed.
# Replicas: max 3 (one per Kafka partition). Scaling beyond 3 gives zero benefit.

resource "kubernetes_deployment" "stream_processor" {
  metadata {
    name      = "stream-processor"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
    labels    = { app = "stream-processor" }
  }

  spec {
    replicas = 1

    selector { match_labels = { app = "stream-processor" } }

    template {
      metadata { labels = { app = "stream-processor" } }

      spec {
        container {
          name              = "stream-processor"
          image             = "sentinel-stream-processor:local"
          image_pull_policy = "Never"

          env {
            name  = "KAFKA_BOOTSTRAP_SERVERS"
            value = "kafka.sentinel-data.svc.cluster.local:9092"
          }
          env {
            name  = "CLASSIFIER_URL"
            value = "http://classifier.sentinel-app.svc.cluster.local:8000"
          }
          env {
            name = "PG_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.app_postgres.metadata[0].name
                key  = "password"
              }
            }
          }
          env {
            name  = "DATABASE_URL"
            value = "postgresql://sentinel:$(PG_PASSWORD)@postgresql.sentinel-data.svc.cluster.local:5432/sentinel"
          }
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
            name  = "SAFE_SAMPLE_RATE"
            value = "0.1"
          }

          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "256Mi" }
          }
        }
      }
    }
  }

  wait_for_rollout = false

  timeouts {
    create = "5m"
    update = "5m"
  }
}
