"""Smoke-test DAG for the Phase 7 Airflow deployment.

Proves the orchestration/ ConfigMap mount and LocalExecutor actually work
before any real pipeline logic depends on them. Superseded by retrain_dag.py
once Phase 7.3 lands — kept as a minimal, fast-running sanity check.
"""

from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def _print_ready() -> None:
    print("Airflow scheduler + LocalExecutor + DAG mount are all working.")


with DAG(
    dag_id="healthcheck",
    description="Confirms the Airflow deployment can load and run a DAG",
    schedule=None,  # manual trigger only — this isn't a real pipeline
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["sentinel", "smoke-test"],
) as dag:
    PythonOperator(
        task_id="print_ready",
        python_callable=_print_ready,
    )
