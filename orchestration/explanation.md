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

## `retrain_dag.py` — the first DAG with real logic

Three tasks, linear dependency chain:

```python
run_retraining >> decide_promotion >> rollout_restart
```

`schedule=None` — manually triggered only, same as `healthcheck`. Today the
trigger source is `services/label-ui`'s "Trigger Retraining" button (`POST
/api/v1/dags/retrain_dag/dagRuns` against Airflow's REST API); later, drift's
PSI signal (once wired in) can trigger the same DAG the same way. The DAG
itself doesn't know or care who called the API — this is why `schedule=None`
plus an external trigger, rather than a `PostgresSensor` polling loop, ended
up being the right shape here: the trigger is an *event* (an operator decided
enough data is labelled), not a *condition* to poll for.

### 1. `run_retraining` — `KubernetesPodOperator`

```python
run_retraining = KubernetesPodOperator(
    task_id="run_retraining",
    namespace="sentinel-pipeline",
    image="sentinel-retraining:local",
    cmds=["python", "-m", "pipelines.retraining"],
    service_account_name="airflow",
    env_vars=[...],           # DATABASE_URL, MONGO_URI, MINIO_*, MLFLOW_TRACKING_URI
    do_xcom_push=True,
    get_logs=True,
    is_delete_operator_pod=True,
    container_resources=k8s.V1ResourceRequirements(
        requests={"cpu": "2", "memory": "4Gi"},
        limits={"cpu": "8", "memory": "8Gi"},
    ),
)
```

Launches a **fresh pod** rather than running the retraining pipeline
in-process as a `PythonOperator` — this is the LocalExecutor caveat from
above in practice: `pipelines.retraining` needs torch/transformers/mlflow,
none of which belong in the Airflow image, and the pod isolates its (large,
variable) resource footprint from the scheduler's own.

`do_xcom_push=True` reads whatever the pod wrote to
`/airflow/xcom/return.json` — `pipelines/retraining/pipeline.py`'s `run()`
writes its final report there via a plain `Path("/airflow/xcom").exists()`
check, so the pipeline package itself has zero Airflow-specific imports and
stays runnable standalone (`python -m pipelines.retraining` from a shell,
no DAG required). See
[`../pipelines/retraining/explanation.md`](../pipelines/retraining/explanation.md)
for what's actually in that report.

**`container_resources` went through three rounds of live tuning**, all
found by actually triggering runs and watching them fail, not by guessing:

- **2Gi/4Gi → OOMKilled** (`exit_code: 137`) around the point fine-tuning
  started. No traceback ever appeared in any log — `SIGKILL` gives a
  process zero chance to flush stdout, so the pod's logs just went silent.
  The real reason was only visible in Airflow's own **persisted** task log
  (`/opt/airflow/logs/dag_id=retrain_dag/run_id=.../task_id=run_retraining/
  attempt=1.log`), which records the pod's final K8s status including
  `reason: OOMKilled` — `kubectl logs`, live or `-f`, showed nothing useful
  for a SIGKILLed process, because there was nothing left to stream.
- **4Gi → 8Gi limit, still OOMKilled.** The actual root cause was in
  `pipelines/retraining/train.py`: every training example was tokenized with
  `padding="max_length"` (a fixed 512 tokens), regardless of its real
  length — wasting enormous memory via attention's O(seq_len²) scaling for
  what are mostly short chat spans. Throwing more memory at it would likely
  have worked eventually, but the actual fix was efficiency
  (`DataCollatorWithPadding`, dynamic per-batch padding), not a bigger pod.
- **CPU limit of 2 cores, benchmark stage pathologically slow (19+ minutes,
  still running).** `pipelines/evaluation/benchmark.py` opens its ONNX
  Runtime session with no `SessionOptions` (see that package's
  explanation.md), so ORT auto-detects thread count from the **host's**
  real CPU count (16, here) rather than the pod's cgroup limit — those
  threads then thrash against a 2-core quota instead of running in
  parallel. Not fixed in `benchmark.py` itself (shared, unmodified code —
  this feature's whole design is "reuse `pipelines/optimizer` and
  `pipelines/evaluation` unchanged"); fixed here instead, by raising the
  pod's CPU limit to 8 so ORT's auto-detected thread count and the cgroup
  limit stop fighting each other.

### 2. `decide_promotion` — `PythonOperator`

```python
def _decide_promotion(**context) -> None:
    report = context["ti"].xcom_pull(task_ids="run_retraining")
    if not report.get("gate_passed"):
        raise ValueError(f"Quality gate failed: {report.get('gate_reasons')}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur.execute("UPDATE model_registry SET status = 'retired' WHERE status = 'active'")
    cur.execute("UPDATE model_registry SET status = 'active', promoted_at = NOW() WHERE model_version = %s", (model_version,))
```

Runs as a scheduler subprocess (LocalExecutor — see above), talking to the
`sentinel` Postgres database directly via `psycopg2` (already bundled in
the official Airflow image, since it's also what the scheduler uses to talk
to its *own* metadata DB — no extra dependency needed). This is the only
place in the entire codebase that writes `model_registry.status = 'active'`,
matching the repo's "Model Registry Source of Truth" rule: pipelines only
ever register as `'staging'`.

**Fails loudly, on purpose, when the quality gate fails.** Raising
`ValueError` marks this task `failed`, which gives `rollout_restart`
`upstream_failed` via Airflow's default `trigger_rule` — it never runs. This
was live-verified as a success case, not a bug: a run fine-tuned on only 40
labelled examples scored 50% accuracy (coin-flip) on the 3780-row held-out
set, `pipelines/evaluation/validate.py` correctly flagged it
(`accuracy 0.5000 below minimum 0.8500`), and this task correctly refused to
touch `model_registry` or restart anything. The system did exactly what
it's for: reject a bad model instead of promoting it.

### 3. `rollout_restart` — `PythonOperator`

```python
def _rollout_restart(**context) -> None:
    from kubernetes import client, config
    config.load_incluster_config()
    apps_v1 = client.AppsV1Api()
    for deployment in ("classifier", "stream-processor"):
        apps_v1.patch_namespaced_deployment(
            name=deployment, namespace="sentinel-app",
            body={"spec": {"template": {"metadata": {"annotations": {"sentinel/restartedAt": ...}}}}},
        )
```

Uses the **Kubernetes Python client** (`kubernetes` package, bundled with
the `apache-airflow-providers-cncf-kubernetes` provider — confirmed present
in the image via `pip list` before writing this, rather than assumed), not
a shelled-out `kubectl rollout restart` — **there is no `kubectl` binary in
the Airflow image** (`which kubectl` exits 1). Patching the pod template's
annotations is exactly what `kubectl rollout restart` does under the hood;
the Python client just does it directly via the API.

This is the payoff for RBAC provisioned a full phase earlier:
`kubernetes_role.airflow_rollout` (`infra/terraform/local/airflow.tf`) grants
the `airflow` ServiceAccount `get/list/patch` on `apps/deployments` in
`sentinel-app` — added back in Phase 7.1 specifically for this step, per
that file's own comment ("granted now so the ServiceAccount doesn't need to
be re-plumbed through the chart values later"). It sat unused until this DAG
finally called it.

### Debugging gotcha: `subPath` ConfigMap mounts don't live-update

Editing `retrain_dag.py` and re-running `terraform apply` updates the
`airflow-dags` ConfigMap correctly, but **the mounted file inside the
scheduler/webserver pods does not change** — confirmed live by `grep`-ing
the file's content inside the pod after an apply that should have changed
it, and seeing the old content. This is the flip side of the
`subPath`-mount fix documented in
[`../infra/terraform/local/explanation.md`](../infra/terraform/local/explanation.md)'s
Airflow gotcha #4: `subPath` mounts deliberately bypass the `..data ->
..<timestamp>` symlink indirection (that's *why* they fix Airflow's DAG
walker), but that same indirection is exactly the mechanism a plain
ConfigMap volume mount uses to pick up changes without a pod restart.
Trading away the symlink to fix the walker bug means trading away
live-updates too. **After any `retrain_dag.py` edit, both the scheduler and
webserver need an explicit restart:**

```bash
kubectl rollout restart statefulset/airflow-scheduler -n sentinel-pipeline
kubectl rollout restart deployment/airflow-webserver -n sentinel-pipeline
```

### Debugging gotcha: reading a live pod's logs vs. Airflow's persisted log

`kubectl logs -f <pod>` looked like it "froze" mid-training on more than one
run — new lines just stopped appearing, no error, connection still open.
Two different real causes produced the same symptom here, which is the
actual lesson: **a frozen-looking log stream is not itself a diagnosis.**

1. Early on, `tqdm`'s default progress bar (used internally by
   `transformers.Trainer`) redraws a single line via `\r` rather than
   emitting newlines — plausible as a cause of confused line-based log
   streaming in a headless pod, so it was disabled
   (`disable_tqdm=True` in `pipelines/retraining/train.py`) as a
   reasonable fix on its own merits.
2. That turned out not to be the actual cause of the "freezing" — the real
   cause (both before and after disabling tqdm) was the OOM kill described
   above. A `SIGKILL`ed process cannot flush a traceback, so of course the
   log stream "freezes" with no error: there is no more output, ever, for
   that container.

The reliable diagnostic, once this was understood, was to stop trusting
live `kubectl logs` output entirely and read Airflow's own **persisted**
task log file directly from inside the scheduler pod — it records the
pod's final Kubernetes status (`exit_code`, `reason: OOMKilled`, etc.)
regardless of whether the pod itself has already been deleted
(`is_delete_operator_pod=True` deletes it quickly after the task instance
finishes):

```bash
kubectl exec -n sentinel-pipeline airflow-scheduler-0 -c scheduler -- \
  cat "/opt/airflow/logs/dag_id=retrain_dag/run_id=<run_id>/task_id=run_retraining/attempt=1.log"
```

---

## `drift_dag.py` — the first DAG that actually runs on a schedule

```python
schedule=timedelta(hours=1)
```

Every other DAG in this repo is `schedule=None` (manually triggered only).
This is the piece CLAUDE.md's data flow describes as "Airflow DAG: triggers
retrain when PSI > 0.2" — it periodically submits the drift Spark job,
reads the result, and calls `retrain_dag` automatically when drift is
found. The manual trigger path (`services/label-ui`'s button) is untouched
by this — `retrain_dag` doesn't know or care which caller hit it.

Six tasks:

```python
submit_drift_job >> wait_for_drift_job
wait_for_drift_job >> cleanup_drift_job
wait_for_drift_job >> check_drift >> [trigger_retrain, no_drift_detected]
```

### Why plain Kubernetes API calls, not `SparkKubernetesOperator`/`SparkKubernetesSensor`

Both classes ship in this image's `apache-airflow-providers-cncf-kubernetes`
install and looked like the obvious choice — submit/watch a
`SparkApplication` CRD Airflow-natively, matching how `run_retraining` uses
`KubernetesPodOperator` for a plain pod. They were tried first, and
abandoned after turning up **three separate undocumented behaviors in one
debugging session**, each independently fixable but adding up to more
friction than the abstraction was worth:

1. **The manifest needs `spec.driver.labels`/`spec.executor.labels`
   present (even `{}`) or the operator's own post-submission bookkeeping
   raises a bare `KeyError`.** Traced to the exact line in
   `custom_object_launcher.py`
   (`labels=self.spark_obj_spec["spec"]["driver"]["labels"]`) by reading
   the installed package's source directly — a first guess at
   `metadata.labels: {}` (the more obvious location) produced the identical
   crash, unchanged.
2. **The operator generates its own resource name from `task_id` + a
   random suffix, ignoring both the constructor's `name=` argument and the
   YAML's own `metadata.name`.** `SparkKubernetesOperator` inherits from
   `KubernetesPodOperator`
   (`SparkKubernetesOperator.__mro__` confirms this), which has its own
   independent pod-naming logic. A sensor built to poll a name assumed to
   be static and known in advance was therefore always polling a resource
   that had never existed — it failed (or, with `soft_fail=True`, silently
   skipped) on its very first poke every single time, and this was easy to
   misread as "the sensor doesn't work" rather than "the sensor is looking
   in the wrong place."
3. **`delete_on_termination=True` deletes the `SparkApplication` the
   instant the *submitting* operator's own `execute()` returns** — which
   is as soon as the driver pod is confirmed *started*
   (`custom_object_launcher.py`'s `spark_job_not_running()` poll loop
   waits for startup, not completion) — not once the actual PySpark job
   finishes. A downstream sensor meant to watch for real completion was
   deleting-out-from-under-itself before it ever got a meaningful chance
   to poll.

After the third fix, the pattern was clear enough to stop debugging the
library and just replace it: `submit_drift_job`, `wait_for_drift_job`, and
`cleanup_drift_job` below are three plain functions using
`kubernetes.client.CustomObjectsApi` directly — the exact same tool
`retrain_dag.py`'s `rollout_restart` already uses successfully for a
different K8s resource. Fewer moving parts, and every line is something
this codebase already understands rather than an opaque provider
abstraction.

```python
def _submit_drift_job(**context) -> str:
    name = f"sentinel-drift-{uuid.uuid4().hex[:8]}"  # unique per run, not static
    manifest = yaml.safe_load(SPARK_APPLICATION_YAML)
    manifest["metadata"]["name"] = name
    api.create_namespaced_custom_object(group="sparkoperator.k8s.io", ..., body=manifest)
    return name  # auto-XComed to the tasks below
```

A **fresh, unique name every run** (not the fixed `sentinel-drift` from
`pipelines/drift/spark-application.yaml`) is deliberate, and fixes a
second, independent bug found along the way: a stale `SparkApplication`
resource left over from earlier manual testing (predating this DAG
entirely) shared that exact static name, and the operator's
`reattach_on_restart` default silently treated it as *this run's* result —
reporting instant "success" without ever submitting a new job. A
UUID-suffixed name makes that class of collision structurally impossible.

```python
def _wait_for_drift_job(**context) -> None:
    name = context["ti"].xcom_pull(task_ids="submit_drift_job")
    while time.monotonic() < deadline:
        state = ...["status"]["applicationState"]["state"]
        if state in ("COMPLETED", "FAILED"):
            return          # never raises — check_drift reads the real truth
        time.sleep(POLL_INTERVAL_S)
```

Never raises on `FAILED` — see the next section for why `FAILED` is
ambiguous at this layer and shouldn't gate anything on its own.
`cleanup_drift_job` (`trigger_rule="all_done"`) always runs afterward
regardless of how this task ends, deleting the named resource so repeated
hourly runs never accumulate leftovers.

### `check_drift` — reads Postgres directly, not the SparkApplication's own status

```python
def _check_drift(**context) -> str:
    cur.execute("SELECT drift_flagged, computed_at, psi FROM drift_stats ORDER BY computed_at DESC LIMIT 1")
    ...
    if age > FRESHNESS_WINDOW:
        return "no_drift_detected"   # "we don't know" must never trigger a retrain
    return "trigger_retrain" if drift_flagged else "no_drift_detected"
```

`pipelines/drift/drift_job.py` deliberately exits with code `2` when drift
**is** detected (not a crash — see that file's own docstring and
explanation.md) and `0` when it ran cleanly and found none. At the
Kubernetes/spark-operator layer, both a genuine crash (exit `1`) and the
intentional exit-`2` "drift found" case surface identically as
`applicationState.state == "FAILED"` — there's no way to tell them apart
from that field alone. `drift_job.py` always calls `write_drift_stats()`
**before** it ever calls `sys.exit()` (in the `0` and `2` cases), so the
Postgres row is the actual source of truth regardless of what the K8s
layer reported. `check_drift` runs with `trigger_rule="all_done"`
specifically so it executes no matter what `wait_for_drift_job` saw, and
does its own freshness check (`FRESHNESS_WINDOW = timedelta(minutes=30)`)
to distinguish "the job ran and found nothing" from "the job never wrote
anything this run" (a real crash, or `drift_job.py`'s own
`MIN_REFERENCE_SIZE`/empty-current-window skip) — either of the latter must
default to *not* retraining, since triggering an unattended fine-tune run
on missing or stale data would be worse than doing nothing.

**Live-verified this exact "job ran, wrote nothing" case, and it wasn't a
bug**: a real end-to-end test run's driver pod completed cleanly in ~8
seconds (checked its actual application logs, not just Airflow's task
log, before `cleanup_drift_job` could delete it) because
`model_registry`'s current `active` row had **zero** matching rows in
`classifications` — `drift_job.py`'s `MIN_REFERENCE_SIZE` guard correctly
refused to compute PSI against an empty baseline and exited `0` without
writing anything. `check_drift` correctly saw a stale `drift_stats` row and
chose `no_drift_detected`. The whole chain worked exactly as designed; the
"no drift" outcome just reflected that the currently-tracked active model
has no real traffic yet, not a flaw in the DAG.

### `trigger_retrain` — `TriggerDagRunOperator`, not an HTTP call

```python
trigger_retrain = TriggerDagRunOperator(task_id="trigger_retrain", trigger_dag_id="retrain_dag")
```

`services/label-ui` has to call Airflow's REST API with Basic auth because
it's a separate process outside Airflow entirely. From *inside* a DAG,
`TriggerDagRunOperator` is the native, in-process way to start another DAG
— no HTTP round-trip, no credentials to manage. Both callers land on the
exact same `retrain_dag`, which has no way to tell them apart and doesn't
need to.
