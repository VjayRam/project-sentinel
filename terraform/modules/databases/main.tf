# ── PostgreSQL migration ConfigMap ────────────────────────────────────────────
# The Bitnami PostgreSQL chart runs any SQL files in this ConfigMap during
# first-boot initialisation (when the data directory is empty). Subsequent
# restarts skip it, so the migration is naturally idempotent.
#
# The "\connect sentinel" line is required because Bitnami's initdb scripts
# execute as the postgres superuser against the default "postgres" database.
# We must explicitly switch to the sentinel database before creating tables.
resource "kubernetes_config_map" "pg_schema" {
  metadata {
    name      = "pg-schema-init"
    namespace = var.namespace
  }

  data = {
    "001_initial_schema.sql" = join("\n", [
      "\\connect sentinel;",
      file("${path.module}/../../../db/postgres/migrations/001_initial_schema.sql"),
    ])
  }
}

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
# Bitnami chart creates:
#   - StatefulSet: postgresql-0
#   - Service:     postgresql          (ClusterIP, port 5432)
#   - Service:     postgresql-hl       (headless, for StatefulSet DNS)
#   - Secret:      postgresql          (contains the password)
#   - PVC:         data-postgresql-0   (2Gi on local-path)
resource "helm_release" "postgresql" {
  name      = "postgresql"
  chart     = "oci://registry-1.docker.io/bitnamicharts/postgresql"
  namespace = var.namespace

  # Wait until the PostgreSQL pod is Ready before Terraform continues.
  # This ensures the DB is accepting connections before the init Job runs.
  wait    = true
  timeout = 300

  values = [yamlencode({
    auth = {
      database = "sentinel"
      username = "sentinel"
      password = var.postgres_password
    }
    primary = {
      initdb = {
        # Point the chart at our ConfigMap. Bitnami runs every .sql and .sh
        # file in this ConfigMap during cluster initialisation.
        scriptsConfigMap = kubernetes_config_map.pg_schema.metadata[0].name
      }
      persistence = {
        size         = "2Gi"
        storageClass = "local-path"
      }
      resources = {
        requests = { memory = "256Mi", cpu = "100m" }
        limits   = { memory = "512Mi", cpu = "500m" }
      }
    }
  })]

  depends_on = [kubernetes_config_map.pg_schema]
}

# ── MongoDB ────────────────────────────────────────────────────────────────────
# MongoDB stores only flagged harmful content (the retraining corpus).
# It is NOT a trace store — Jaeger handles traces.
# Bitnami chart creates:
#   - StatefulSet: mongodb-0
#   - Service:     mongodb             (ClusterIP, port 27017)
#   - PVC:         datadir-mongodb-0   (2Gi on local-path)
resource "helm_release" "mongodb" {
  name      = "mongodb"
  chart     = "oci://registry-1.docker.io/bitnamicharts/mongodb"
  namespace = var.namespace

  wait    = true
  timeout = 300

  values = [yamlencode({
    auth = {
      enabled  = true
      database = "sentinel"
      username = "sentinel"
      password = var.mongodb_password
    }
    architecture = "standalone"
    persistence = {
      size         = "2Gi"
      storageClass = "local-path"
    }
    resources = {
      requests = { memory = "256Mi", cpu = "100m" }
      limits   = { memory = "512Mi", cpu = "500m" }
    }
  })]
}
