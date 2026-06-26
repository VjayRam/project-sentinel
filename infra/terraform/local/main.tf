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
