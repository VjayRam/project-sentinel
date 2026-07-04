"""Drift detection → conditional retrain trigger.

Runs on a schedule (unlike healthcheck/retrain_dag, both schedule=None):
submits the same SparkApplication CRD pipelines/drift/spark-application.yaml
defines, waits for it to reach a terminal state, then reads the freshly-
written drift_stats row directly from Postgres and triggers retrain_dag if
drift_flagged is true.

This is the piece CLAUDE.md's data flow describes as "Airflow DAG: triggers
retrain when PSI > 0.2" — previously only reachable by a human clicking
"Trigger Retraining" in services/label-ui. That manual path still exists
and is unaffected; this DAG is a second, automatic caller of the exact same
retrain_dag, via Airflow's own cross-DAG trigger mechanism rather than an
HTTP call (label-ui has to use HTTP + Basic auth because it's a separate
process outside Airflow; from inside Airflow, TriggerDagRunOperator is the
native way to do the same thing).

Submits/polls/deletes the SparkApplication via the plain Kubernetes Python
client (same pattern as retrain_dag.py's rollout_restart) rather than
apache-airflow-providers-cncf-kubernetes' SparkKubernetesOperator/
SparkKubernetesSensor, despite both being available in this image. Tried
those first; abandoned after they turned up three separate undocumented
quirks in a single debugging session: (1) the manifest must carry
spec.driver.labels/spec.executor.labels or the operator's own post-
submission bookkeeping raises a bare KeyError, unrelated to whether the
actual Spark job runs fine; (2) SparkKubernetesOperator's execute() is
inherited from KubernetesPodOperator, which generates its own pod/resource
name from task_id + a random suffix regardless of the name= constructor
arg or the YAML's own metadata.name — so a downstream sensor built to poll
a predictable, static name never finds the actual submitted resource;
(3) delete_on_termination=True deletes the resource the instant the
*submitting* operator's own execute() returns (right after the driver pod
is confirmed started), not after the job actually finishes — deleting it
out from under any downstream consumer. Each was individually fixable, but
three independent surprises from one library in one sitting is a signal to
use a smaller, fully-understood tool instead: three plain functions using
the same kubernetes.client the rest of this repo's Kubernetes-facing DAG
code already uses.

Why the branch reads drift_stats directly instead of trusting the
SparkApplication's own pass/fail status: pipelines/drift/drift_job.py
deliberately exits with code 2 when drift IS detected (not a crash — see
that file's docstring) and only 0 when everything ran cleanly with no
drift. spark-operator's CRD status reports both as "FAILED" (2) vs
"COMPLETED" (0) — a real crash (config/DB error, exit 1) also shows FAILED,
indistinguishable from "drift detected" at that layer. drift_job.py always
calls write_drift_stats() BEFORE it ever calls sys.exit() (0 and 2 alike),
so the Postgres row is the actual source of truth regardless of how the
K8s layer classified the pod — check_drift runs with trigger_rule="all_done"
specifically so it executes no matter what state the job ended up in, and
does its own freshness check to tell "job genuinely didn't run" (a real
crash, or drift_job.py's MIN_REFERENCE_SIZE/empty-window skip) apart from
"job ran and found no drift."
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import yaml
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

logger = logging.getLogger(__name__)

NAMESPACE = "sentinel-pipeline"
API_GROUP = "sparkoperator.k8s.io"
API_VERSION = "v1beta2"
PLURAL = "sparkapplications"

# Mirrors pipelines/drift/spark-application.yaml — kept as a second copy
# rather than a shared file because orchestration/'s ConfigMap mount
# (infra/terraform/local/airflow.tf) only picks up orchestration/*.py, and
# the YAML file isn't mounted into the Airflow pods. If the manifest
# changes, update both. metadata.name is overwritten at submission time
# with a per-run unique name (see _submit_drift_job) — the value here is a
# placeholder.
SPARK_APPLICATION_YAML = """
apiVersion: sparkoperator.k8s.io/v1beta2
kind: SparkApplication
metadata:
  name: sentinel-drift
  namespace: sentinel-pipeline
spec:
  type: Python
  pythonVersion: "3"
  mode: cluster
  image: sentinel-drift:local
  imagePullPolicy: Never
  mainApplicationFile: local:///opt/spark/work-dir/drift_job.py
  arguments:
    - "--hours"
    - "24"
    - "--reference-size"
    - "1000"
  sparkVersion: "3.5.3"
  restartPolicy:
    type: Never
  driver:
    cores: 1
    coreLimit: "1200m"
    memory: "512m"
    serviceAccount: spark
    env:
      - name: DATABASE_URL
        valueFrom:
          secretKeyRef:
            name: drift-postgres
            key: database-url
      - name: PYSPARK_PYTHON
        value: /usr/bin/python3
      - name: PYSPARK_DRIVER_PYTHON
        value: /usr/bin/python3
  executor:
    cores: 1
    instances: 2
    memory: "512m"
    env:
      - name: PYSPARK_PYTHON
        value: /usr/bin/python3
"""

# How old a drift_stats row can be and still count as "this run's result."
# The drift Spark job (small dataset, 2 executors) typically finishes in
# well under this window — generous on purpose so a slow node doesn't cause
# a false "job didn't run" verdict.
FRESHNESS_WINDOW = timedelta(minutes=30)

# Bounds how long wait_for_drift_job polls before giving up. The one
# pre-existing successful manual run (before this DAG existed) took ~42s
# end to end for 1 driver + 2 executors — generous multiple of that.
POLL_TIMEOUT = timedelta(minutes=10)
POLL_INTERVAL_S = 15


def _submit_drift_job(**context) -> str:
    from kubernetes import client, config

    config.load_incluster_config()
    api = client.CustomObjectsApi()

    manifest = yaml.safe_load(SPARK_APPLICATION_YAML)
    # A fresh name every run, not the static name from the YAML — avoids
    # both known failure modes of a fixed name: colliding with a leftover
    # from a previous run that didn't clean up in time, and (as this repo
    # briefly used) an operator's reattach-by-name logic silently skipping
    # a genuinely new submission because something with that name already
    # existed and looked "done."
    name = f"sentinel-drift-{uuid.uuid4().hex[:8]}"
    manifest["metadata"]["name"] = name

    api.create_namespaced_custom_object(
        group=API_GROUP, version=API_VERSION, namespace=NAMESPACE, plural=PLURAL, body=manifest
    )
    logger.info("Submitted SparkApplication %s", name)
    return name  # auto-XComed


def _wait_for_drift_job(**context) -> None:
    from kubernetes import client, config

    name = context["ti"].xcom_pull(task_ids="submit_drift_job")

    config.load_incluster_config()
    api = client.CustomObjectsApi()

    deadline = time.monotonic() + POLL_TIMEOUT.total_seconds()
    while time.monotonic() < deadline:
        try:
            obj = api.get_namespaced_custom_object_status(
                group=API_GROUP, version=API_VERSION, namespace=NAMESPACE, plural=PLURAL, name=name
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                logger.warning("SparkApplication %s not found yet — waiting", name)
                time.sleep(POLL_INTERVAL_S)
                continue
            raise

        state = obj.get("status", {}).get("applicationState", {}).get("state")
        if state in ("COMPLETED", "FAILED"):
            # FAILED is not necessarily an error — see module docstring.
            # This task never raises on it; check_drift reads the actual
            # truth from Postgres regardless of which terminal state this
            # was.
            logger.info("SparkApplication %s reached terminal state: %s", name, state)
            return
        logger.info("SparkApplication %s still running (state=%s)", name, state or "SUBMITTED")
        time.sleep(POLL_INTERVAL_S)

    logger.warning("Timed out after %s waiting for SparkApplication %s", POLL_TIMEOUT, name)


def _cleanup_drift_job(**context) -> None:
    from kubernetes import client, config

    name = context["ti"].xcom_pull(task_ids="submit_drift_job")
    if not name:
        return

    config.load_incluster_config()
    api = client.CustomObjectsApi()
    try:
        api.delete_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION, namespace=NAMESPACE, plural=PLURAL, name=name
        )
        logger.info("Deleted SparkApplication %s", name)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        logger.info("SparkApplication %s already gone", name)


def _check_drift(**context) -> str:
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT drift_flagged, computed_at, psi FROM drift_stats "
                "ORDER BY computed_at DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        logger.warning("No drift_stats rows exist yet — treating as no drift")
        return "no_drift_detected"

    drift_flagged, computed_at, psi = row
    age = datetime.now(timezone.utc) - computed_at
    if age > FRESHNESS_WINDOW:
        # The drift job didn't write anything new this run — either it hit
        # drift_job.py's MIN_REFERENCE_SIZE guard, found no current-window
        # rows, or genuinely crashed before reaching write_drift_stats().
        # Any of those means "we don't know," and "we don't know" must
        # never trigger a retrain.
        logger.warning(
            "Latest drift_stats row is stale (age=%s > %s) — drift job likely "
            "didn't write new data this run; not triggering retrain",
            age,
            FRESHNESS_WINDOW,
        )
        return "no_drift_detected"

    logger.info("Latest drift_stats | psi=%.4f | drift_flagged=%s", psi, drift_flagged)
    return "trigger_retrain" if drift_flagged else "no_drift_detected"


with DAG(
    dag_id="drift_dag",
    description="Periodically checks for input distribution drift and triggers retrain_dag if found",
    schedule=timedelta(hours=1),
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["sentinel", "drift"],
) as dag:
    submit_drift_job = PythonOperator(
        task_id="submit_drift_job",
        python_callable=_submit_drift_job,
    )

    wait_for_drift_job = PythonOperator(
        task_id="wait_for_drift_job",
        python_callable=_wait_for_drift_job,
        trigger_rule="all_done",
    )

    cleanup_drift_job = PythonOperator(
        task_id="cleanup_drift_job",
        python_callable=_cleanup_drift_job,
        trigger_rule="all_done",  # always clean up, whatever happened above
    )

    check_drift = BranchPythonOperator(
        task_id="check_drift",
        python_callable=_check_drift,
        trigger_rule="all_done",
    )

    trigger_retrain = TriggerDagRunOperator(
        task_id="trigger_retrain",
        trigger_dag_id="retrain_dag",
    )

    no_drift_detected = EmptyOperator(task_id="no_drift_detected")

    submit_drift_job >> wait_for_drift_job
    wait_for_drift_job >> cleanup_drift_job
    wait_for_drift_job >> check_drift >> [trigger_retrain, no_drift_detected]
