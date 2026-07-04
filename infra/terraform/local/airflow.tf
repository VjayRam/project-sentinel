# ── Airflow (Phase 7: orchestration) ──────────────────────────────────────────
# LocalExecutor — tasks run as subprocesses of the scheduler pod. No Celery/
# Redis/Flower needed at this scale, matching the project's "don't add
# infrastructure beyond what's needed" principle (same reasoning as pinning
# spark-operator's job namespace instead of a cluster-wide install).
#
# Reuses the existing PostgreSQL instance (separate "airflow" database, see
# main.tf's postgres_init 02_airflow_db.sql) instead of the chart's bundled
# Postgres subchart — one fewer stateful thing to operate locally.
#
# DAGs are mounted from a ConfigMap built from orchestration/*.py rather than
# git-sync or a custom image — same pattern main.tf already uses for
# Prometheus/Grafana config and the Postgres init scripts (Terraform reads
# the repo file, embeds it, mounts it). Simplest option that doesn't require
# rebuilding an image or recreating the k3d cluster with a host volume mount
# every time a DAG file changes.

locals {
  dag_files = fileset("${path.module}/../../../orchestration", "*.py")

  # Mounted per-file via subPath rather than mounting the ConfigMap as a
  # whole directory. Kubernetes mounts ConfigMap volumes through a
  # "..data -> ..<timestamp>" symlink indirection to update them atomically
  # — Airflow's DAG-directory walker (find_path_from_directory) doesn't
  # handle that structure and aborts with "Detected recursive loop when
  # walking DAG directory" (reproduced live). subPath mounts bypass that
  # indirection entirely and appear as plain files, at the cost of one
  # volumeMount per DAG file instead of one mount for the whole folder.
  dag_volume_mounts = [
    for f in local.dag_files : {
      name      = "dags"
      mountPath = "/opt/airflow/dags/${f}"
      subPath   = f
      readOnly  = true
    }
  ]
}

resource "kubernetes_config_map" "airflow_dags" {
  metadata {
    name      = "airflow-dags"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }

  data = {
    for f in local.dag_files :
    f => file("${path.module}/../../../orchestration/${f}")
  }
}

# Static Flask secret key for the webserver (signs session cookies). Without
# this, the chart auto-generates a fresh random key on every deploy — every
# `terraform apply` that touches the release invalidates all sessions and
# the UI shows a persistent "dynamic webserver secret key" warning banner.
# Key name inside the secret (webserver-secret-key) is fixed by the chart —
# see templates/_helpers.yaml's webserver_secret_key_secret definition.
resource "kubernetes_secret" "airflow_webserver_secret_key" {
  metadata {
    # Not "airflow-webserver-secret-key" — the chart's own auto-generated
    # secret (created back when webserverSecretKeySecretName was unset) is
    # already sitting at that exact name (its naming convention is
    # {{ airflow.fullname }}-webserver-secret-key). A different name avoids
    # the collision; the chart stops rendering its own version of this
    # resource once webserverSecretKeySecretName is set below, and Helm
    # prunes the now-orphaned one on this upgrade.
    name      = "airflow-webserver-secret-key-static"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  data = {
    webserver-secret-key = var.airflow_webserver_secret_key
  }
}

# Scheduler's identity — LocalExecutor runs tasks as scheduler subprocesses,
# so this is the identity that needs permission to trigger a rollout restart
# (added once the retrain DAG's promotion task lands in Phase 7.3; granted
# now so the ServiceAccount doesn't need to be re-plumbed through the chart
# values later).
resource "kubernetes_service_account" "airflow" {
  metadata {
    name      = "airflow"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

# Cross-namespace binding: the Role lives where the permission applies
# (sentinel-app), the ServiceAccount lives where Airflow actually runs
# (sentinel-pipeline) — RoleBinding subjects support a different namespace
# than the Role itself.
resource "kubernetes_role" "airflow_rollout" {
  metadata {
    name      = "airflow-rollout"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "patch"]
  }
}

resource "kubernetes_role_binding" "airflow_rollout" {
  metadata {
    name      = "airflow-rollout"
    namespace = kubernetes_namespace.sentinel["sentinel-app"].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.airflow_rollout.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.airflow.metadata[0].name
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

# Phase 7.3: retrain_dag.py's run_retraining task uses KubernetesPodOperator
# to launch the sentinel-retraining pod — needs permission to create/watch/
# delete pods (+ read their logs) in its own namespace. Same shape as
# main.tf's spark_driver role, just for the airflow SA instead of spark.
resource "kubernetes_role" "airflow_pod_launcher" {
  metadata {
    name      = "airflow-pod-launcher"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["create", "get", "list", "watch", "delete"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods/log"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_role_binding" "airflow_pod_launcher" {
  metadata {
    name      = "airflow-pod-launcher"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.airflow_pod_launcher.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.airflow.metadata[0].name
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

# Phase 7.4: drift_dag.py submits/polls/deletes the sentinel-drift-<uuid>
# SparkApplication via plain kubernetes.client.CustomObjectsApi calls — a
# different API group (sparkoperator.k8s.io, the custom resource itself)
# from airflow_pod_launcher above (core API pods, for the retraining
# KubernetesPodOperator). The spark ServiceAccount's own Role (main.tf's
# spark_driver) covers what the DRIVER pod needs to manage its executors —
# this is a separate concern: what the AIRFLOW SA needs to create/watch/
# delete the CR that spark-operator's controller then acts on.
resource "kubernetes_role" "airflow_spark_application" {
  metadata {
    name      = "airflow-spark-application"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  rule {
    api_groups = ["sparkoperator.k8s.io"]
    resources  = ["sparkapplications", "sparkapplications/status"]
    verbs      = ["create", "get", "list", "watch", "delete"]
  }
}

resource "kubernetes_role_binding" "airflow_spark_application" {
  metadata {
    name      = "airflow-spark-application"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.airflow_spark_application.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.airflow.metadata[0].name
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
}

# Mirrors Mongo/MinIO credentials into sentinel-pipeline for
# run_retraining's pod env (K8s secrets are namespace-scoped — same
# reasoning as main.tf's app_mongodb/app_minio mirrors into sentinel-app).
resource "kubernetes_secret" "retraining_mongo" {
  metadata {
    name      = "retraining-mongo"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  data = {
    mongo-uri = "mongodb://sentinel:${var.mongodb_password}@mongodb.sentinel-data.svc.cluster.local:27017/sentinel"
  }
}

resource "kubernetes_secret" "retraining_minio" {
  metadata {
    name      = "retraining-minio"
    namespace = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  }
  data = {
    root-user     = var.minio_root_user
    root-password = var.minio_root_password
  }
}

resource "helm_release" "airflow" {
  name       = "airflow"
  repository = "https://airflow.apache.org"
  chart      = "airflow"
  # Pinned to an exact version, not a "~>" constraint — the Helm provider
  # resolves constraints to a concrete version during apply, which produced
  # a "Provider produced inconsistent final plan" error (a known provider
  # quirk) when combined with importing an existing release. Exact version
  # sidesteps it and matches CLAUDE.md's "never :latest, always pinned"
  # spirit anyway.
  version          = "1.15.0"
  namespace        = kubernetes_namespace.sentinel["sentinel-pipeline"].metadata[0].name
  create_namespace = false
  wait             = true
  timeout          = 600 # DB migration Job + scheduler + webserver take a while on first install

  values = [yamlencode({
    executor = "LocalExecutor"

    webserverSecretKeySecretName = kubernetes_secret.airflow_webserver_secret_key.metadata[0].name

    # Explicit, not trusting the chart/image default — services/label-ui
    # authenticates to the REST API (POST .../dagRuns) with the same
    # admin/<password> credentials the webserver UI login already uses.
    # Without this, the stable API's default auth backend rejects Basic
    # auth even though the webserver's own login page accepts it.
    config = {
      api = {
        auth_backends = "airflow.api.auth.backend.basic_auth"
      }
    }

    # No Celery in this phase — LocalExecutor doesn't use them.
    redis  = { enabled = false }
    flower = { enabled = false }
    statsd = { enabled = false }
    # No deferrable operators used yet — skip the extra pod.
    triggerer = { enabled = false }

    # Explicit — the scheduler/webserver's wait-for-airflow-migrations init
    # container crash-loops forever if this Job never gets created. Live-
    # verified this doesn't happen reliably on its own with an external
    # (non-subchart) postgresql, so it's set explicitly rather than trusting
    # the chart default.
    migrateDatabaseJob = {
      enabled      = true
      useHelmHooks = false
    }

    postgresql = { enabled = false }
    data = {
      metadataConnection = {
        user     = "sentinel"
        pass     = var.postgres_password
        protocol = "postgresql"
        host     = "postgresql.sentinel-data.svc.cluster.local"
        port     = 5432
        db       = "airflow"
        sslmode  = "disable"
      }
    }

    dags = {
      gitSync     = { enabled = false }
      persistence = { enabled = false }
    }

    # extraVolumes/extraVolumeMounts are per-component in this chart (under
    # scheduler/webserver/workers/triggerer), not a top-level key — an
    # earlier version of this config set them at the top level, which the
    # chart silently ignored (no error, just an empty /opt/airflow/dags on
    # every pod). Mounted on both scheduler (parses + executes DAGs) and
    # webserver (renders the DAG list/graph in the UI), one subPath mount
    # per file (local.dag_volume_mounts) — see that local's comment for why.
    scheduler = {
      serviceAccount = {
        create = false
        name   = kubernetes_service_account.airflow.metadata[0].name
      }
      extraVolumes = [{
        name = "dags"
        configMap = {
          name = kubernetes_config_map.airflow_dags.metadata[0].name
        }
      }]
      extraVolumeMounts = local.dag_volume_mounts
      # retrain_dag.py's decide_promotion and drift_dag.py's check_drift
      # both read this directly (os.environ["DATABASE_URL"]) to talk to the
      # "sentinel" database (model_registry, drift_stats) — NOT
      # data.metadataConnection above, which points at Airflow's own
      # separate "airflow" database. Reuses the same drift-postgres secret
      # already mirrored into this namespace for the drift SparkApplication's
      # driver pod. Scheduler only — DAG *parsing* never touches Postgres,
      # only task *execution* (a scheduler subprocess under LocalExecutor)
      # does. (drift_dag.py's Kubernetes access goes through plain
      # kubernetes.config.load_incluster_config(), the same as
      # retrain_dag.py's rollout_restart — no Airflow Connection object
      # needed for that, unlike an earlier version of this DAG that used
      # apache-airflow-providers-cncf-kubernetes' SparkKubernetesOperator/
      # Sensor and their KubernetesHook-based "kubernetes_default"
      # connection; see drift_dag.py's module docstring for why that was
      # abandoned.)
      env = [{
        name = "DATABASE_URL"
        valueFrom = {
          secretKeyRef = {
            name = kubernetes_secret.drift_postgres.metadata[0].name
            key  = "database-url"
          }
        }
      }]
    }

    webserver = {
      defaultUser = {
        enabled   = true
        username  = "admin"
        password  = var.airflow_admin_password
        role      = "Admin"
        email     = "admin@sentinel.local"
        firstName = "Sentinel"
        lastName  = "Admin"
      }
      extraVolumes = [{
        name = "dags"
        configMap = {
          name = kubernetes_config_map.airflow_dags.metadata[0].name
        }
      }]
      extraVolumeMounts = local.dag_volume_mounts
    }
  })]

  depends_on = [
    kubernetes_config_map.airflow_dags,
    kubernetes_secret.airflow_webserver_secret_key,
  ]
}
