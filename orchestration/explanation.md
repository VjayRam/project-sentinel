# Orchestration (Airflow DAGs) — Explanation

This directory holds Airflow DAG definitions. There is no Python package
structure, no `__init__.py`, no local dependency management — every `.py`
file here is read directly by `infra/terraform/local/airflow.tf`, embedded
into a `kubernetes_config_map`, and mounted into the scheduler/webserver
pods. See [`../infra/terraform/local/explanation.md`](../infra/terraform/local/explanation.md)'s
Airflow section for exactly how that mount works and the gotchas involved in
getting it right — this file focuses on writing and operating DAGs, not on
the deployment mechanism.

---

## Why `orchestration/`, not `dags/`

Airflow's own convention calls this a "dags folder," and most tutorials name
the directory `dags/`. This repo uses `orchestration/` instead — it reads
more clearly as "the thing that orchestrates `pipelines/`" alongside
`services/` and `pipelines/` at the repo root, and avoids the slightly
confusing stutter of `orchestration/dags/dags_folder.py`. Functionally
identical; Airflow doesn't care what the directory is called, only what's
mounted at `/opt/airflow/dags` inside the pods.

---

## `healthcheck_dag.py`

```python
with DAG(
    dag_id="healthcheck",
    schedule=None,          # manual trigger only
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=["sentinel", "smoke-test"],
) as dag:
    PythonOperator(task_id="print_ready", python_callable=_print_ready)
```

Exists purely to prove the deployment mechanism works — that a `.py` file
placed here actually gets mounted, parsed with zero import errors, and can
run to completion — before any real pipeline logic (the eventual
`retrain_dag.py`) depends on the same mechanism. `schedule=None` means it
never runs on its own; it only executes when triggered manually or via CI.

**`catchup=False`** matters even for a manually-triggered DAG: without it,
Airflow would try to backfill every scheduled interval between `start_date`
and now the first time the DAG is unpaused — for a `schedule=None` DAG this
is a no-op, but it's worth knowing for whatever DAG replaces this one on an
actual schedule (`PostgresSensor` polling on a `timedelta`, per the retrain
DAG design).

---

## Operating DAGs from the CLI

`dev-start.sh` verifies DAGs load correctly on every run, but here's what it
does, spelled out, for when something needs debugging manually:

```bash
# List every DAG Airflow has found. fileloc shows the exact mounted path —
# useful for confirming a new file actually landed where expected.
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags list

# The single most useful command when a DAG "isn't showing up": confirms
# whether it parsed at all, and if not, why. "No data found" == clean.
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags list-import-errors

# DAGs start paused by default — unpause before a scheduled run will ever fire.
# Not needed for a manual trigger (see below), only for schedule!=None DAGs.
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags unpause <dag_id>

# Manually trigger a run right now, regardless of schedule.
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags trigger <dag_id>

# Check run history / final state (success, failed, running).
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow dags list-runs -d <dag_id>

# Per-task state within a specific run — the run_id comes from list-runs above.
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  airflow tasks states-for-dag-run <dag_id> "<run_id>"
```

**Why `-c scheduler`?** The `airflow-scheduler-0` pod runs two containers:
`scheduler` (the actual scheduler process, where the CLI and DAG parsing
live) and `scheduler-log-groomer` (a sidecar that periodically deletes old
log files). Every `airflow` CLI command needs to target the `scheduler`
container explicitly — `kubectl exec` picks the first container by default,
which may not be the right one.

**Faster than the UI for scripting/CI** — every command above is exactly
what a shell script (or `dev-start.sh`) can poll and assert on, without
needing to drive a browser. The UI (`http://localhost:8090`, see the infra
explanation.md's port-forward gotcha for why it's 8090 not 8080) is better
for visually inspecting the DAG graph, Gantt charts, and log output.

---

## LocalExecutor semantics, in case you're used to Celery/Kubernetes executors

Every task in every DAG runs as a **subprocess of the scheduler pod itself**
— there's no separate worker pod, no task queue, no message broker. This
means:

- **A task's resource usage counts against the scheduler pod's limits.** A
  memory-hungry task (e.g., the eventual retrain DAG's model-loading step)
  competes with the scheduler process's own memory, not an isolated worker.
  For heavier pipeline steps, prefer `KubernetesPodOperator` (runs the task
  in its own pod, using the existing `sentinel-drift:local` /
  `sentinel-optimizer` images) over a plain `PythonOperator` that imports and
  runs pipeline code in-process.
- **Parallelism is bounded by the scheduler pod's CPU**, not by the number of
  workers you can scale out. Fine for this project's current scale (one
  pipeline run at a time); would need `KubernetesExecutor` or
  `CeleryExecutor` to genuinely parallelize across nodes.
- **No task queue to inspect** — if a task should be running and isn't,
  the answer is always "check the scheduler pod's logs and process table,"
  not "check a Redis/RabbitMQ queue depth."

---

## What's next (Phase 7.3)

`retrain_dag.py` (not yet written) will be the first DAG with real logic:
`PostgresSensor` (in `reschedule` mode, not `poke` — see the repo root
CLAUDE.md's "Common Interview Points") waits for new `classifications` rows,
runs the drift job via `KubernetesPodOperator` against `sentinel-drift:local`,
branches on its exit code (2 = drift detected), and on that branch runs the
optimizer + evaluation pipelines before promoting a model — the first thing
in this whole codebase to actually flip `model_registry.status` to
`'active'`. See [`../pipelines/drift/explanation.md`](../pipelines/drift/explanation.md)
and [`../pipelines/evaluation/explanation.md`](../pipelines/evaluation/explanation.md)
for what those two pipeline steps actually do once wired in.
