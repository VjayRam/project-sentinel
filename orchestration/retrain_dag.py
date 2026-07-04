"""Retraining DAG — triggered by services/label-ui's "Trigger Retraining"
button (POST /api/v1/dags/retrain_dag/dagRuns) today, and by drift's future
PSI signal later; this DAG doesn't care who calls the API (schedule=None).

Three tasks:
  1. run_retraining   — KubernetesPodOperator launches sentinel-retraining:local
                         (pipelines/retraining), which fine-tunes, logs to
                         MLflow, and reuses pipelines/optimizer + evaluation
                         unchanged to register a new 'staging' model_registry
                         row. Its report.json comes back via XCom (the pod
                         writes it to /airflow/xcom/return.json, the sidecar
                         this operator provisions tails that path).
  2. decide_promotion — reads the XCom report; on gate_passed, promotes the
                         new model_version to 'active' (retiring the old
                         one) directly via psycopg2 — model_registry is the
                         single source of truth per CLAUDE.md, and only
                         Airflow writes to it. On failure this task raises,
                         so rollout_restart's default trigger_rule skips it.
  3. rollout_restart  — patches classifier + stream-processor Deployments'
                         pod-template annotations via the Kubernetes Python
                         client (no kubectl binary in this image — verified
                         live) — same effect as `kubectl rollout restart`.
                         Uses kubernetes_role.airflow_rollout, RBAC that was
                         provisioned back in Phase 7.1 specifically for this
                         step (see infra/terraform/local/airflow.tf).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from kubernetes.client import models as k8s

logger = logging.getLogger(__name__)

PIPELINE_NAMESPACE = "sentinel-pipeline"
APP_NAMESPACE = "sentinel-app"


def _decide_promotion(**context) -> None:
    import psycopg2

    report = context["ti"].xcom_pull(task_ids="run_retraining")
    if not report:
        raise RuntimeError("run_retraining produced no XCom report")
    if isinstance(report, str):
        report = json.loads(report)

    if not report.get("gate_passed"):
        raise ValueError(f"Quality gate failed: {report.get('gate_reasons')}")

    model_version = report["model_version"]
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE model_registry SET status = 'retired' WHERE status = 'active'")
            cur.execute(
                "UPDATE model_registry SET status = 'active', promoted_at = NOW() "
                "WHERE model_version = %s",
                (model_version,),
            )
        conn.commit()
    finally:
        conn.close()

    logger.info("Promoted model_version=%s to active", model_version)
    context["ti"].xcom_push(key="promoted_version", value=model_version)


def _rollout_restart(**context) -> None:
    from kubernetes import client, config

    config.load_incluster_config()
    apps_v1 = client.AppsV1Api()
    restarted_at = datetime.now(timezone.utc).isoformat()

    for deployment in ("classifier", "stream-processor"):
        patch = {
            "spec": {"template": {"metadata": {"annotations": {"sentinel/restartedAt": restarted_at}}}}
        }
        apps_v1.patch_namespaced_deployment(name=deployment, namespace=APP_NAMESPACE, body=patch)
        logger.info("Restarted deployment/%s", deployment)


with DAG(
    dag_id="retrain_dag",
    description="Fine-tune, evaluate, and (if it passes the quality gate) promote a new model",
    schedule=None,  # manually triggered — by services/label-ui today, drift's PSI signal later
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["sentinel", "retraining"],
) as dag:
    run_retraining = KubernetesPodOperator(
        task_id="run_retraining",
        name="sentinel-retraining",
        namespace=PIPELINE_NAMESPACE,
        image="sentinel-retraining:local",
        image_pull_policy="Never",
        cmds=["python", "-m", "pipelines.retraining"],
        arguments=["--output-dir", "/tmp/artifacts", "--log-dir", "/tmp/logs"],
        service_account_name="airflow",
        env_vars=[
            k8s.V1EnvVar(
                name="DATABASE_URL",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="drift-postgres", key="database-url")
                ),
            ),
            k8s.V1EnvVar(
                name="MONGO_URI",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="retraining-mongo", key="mongo-uri")
                ),
            ),
            k8s.V1EnvVar(
                name="MINIO_ENDPOINT",
                value="http://minio.sentinel-data.svc.cluster.local:9000",
            ),
            k8s.V1EnvVar(
                name="MINIO_ACCESS_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="retraining-minio", key="root-user")
                ),
            ),
            k8s.V1EnvVar(
                name="MINIO_SECRET_KEY",
                value_from=k8s.V1EnvVarSource(
                    secret_key_ref=k8s.V1SecretKeySelector(name="retraining-minio", key="root-password")
                ),
            ),
            k8s.V1EnvVar(
                name="MLFLOW_TRACKING_URI",
                value="http://mlflow.sentinel-monitoring.svc.cluster.local:5000",
            ),
        ],
        do_xcom_push=True,
        get_logs=True,
        is_delete_operator_pod=True,
        in_cluster=True,
        startup_timeout_seconds=600,
        # 2Gi/4Gi OOM-killed the pod within ~2 minutes of the training loop
        # starting (live-reproduced repeatedly — same signature as mlflow's
        # earlier OOM: crashes silently right as the heavy step begins, no
        # traceback since SIGKILL gives the process no chance to flush
        # stdout). Node has ample headroom (~10Gi free) — this is a cgroup
        # limit problem, not real contention. RoBERTa-base fine-tuning
        # (weights + AdamW optimizer state + gradients + activations for
        # batch_size=8 x seq_len=512, all fp32 on CPU) plus a second full
        # train-set forward pass per epoch (_TrainMetricsCallback) needs
        # more headroom than a lean inference-only pod like the classifier.
        # cpu limit=2 made the benchmark stage pathologically slow (still
        # running after 19+ minutes, live-reproduced) — pipelines/evaluation/
        # benchmark.py opens its ONNX Runtime session with no SessionOptions,
        # so ORT auto-detects thread count from the host's real CPU count
        # (16 here), not the pod's cgroup limit. Those threads then thrash
        # against a 2-core quota instead of running in parallel. Not fixed
        # in benchmark.py itself (shared, unmodified pipeline code per this
        # feature's design — "rest of the pipeline remains the same");
        # fixed here instead by giving the pod enough real cores that ORT's
        # thread count and the cgroup limit stop fighting each other. Host
        # has 16 cores with room to spare.
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "2", "memory": "4Gi"},
            limits={"cpu": "8", "memory": "8Gi"},
        ),
    )

    decide_promotion = PythonOperator(
        task_id="decide_promotion",
        python_callable=_decide_promotion,
    )

    rollout_restart = PythonOperator(
        task_id="rollout_restart",
        python_callable=_rollout_restart,
    )

    run_retraining >> decide_promotion >> rollout_restart
