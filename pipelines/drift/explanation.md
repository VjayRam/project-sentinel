# Drift Detection Pipeline — Explanation

A one-shot PySpark job (`drift_job.py`) that compares the classifier's recent
score distribution against a reference baseline, computes PSI and JSD, writes
the result to `drift_stats`, and exits with a status code Airflow can branch
on. Runs to completion and exits — this is a K8s `Job` (via the
`SparkApplication` CRD), not a `Deployment`, matching the repo's
service-vs-job split described in the root `CLAUDE.md`.

**Phase 7.4 update**: this job now runs automatically, hourly, via
[`../../orchestration/drift_dag.py`](../../orchestration/explanation.md) —
`spark-application.yaml` in this directory is still the reference manifest
(and still useful for a manual `kubectl apply -f` test independent of
Airflow), but the actual submitted resource each hour is a copy of it with
a unique generated name, created directly via the Kubernetes API rather
than this file. See that DAG's explanation for the full story, including
why the higher-level `SparkKubernetesOperator`/`SparkKubernetesSensor`
Airflow operators were tried first and abandoned.

---

## Why PySpark for what's currently a few thousand floats

At today's data volume this could be a 20-line `numpy` script. It's built on
Spark on purpose — it's the deliberate "learn distributed data processing"
phase of this project (see root `CLAUDE.md`'s phase table), and writing the
binning/aggregation as genuine Spark DataFrame operations (not
`.toPandas()` immediately) is what makes `.explain()` on the physical plan
meaningful practice. The design constraint that keeps it honest: **only the
final 10-bin aggregate (10 rows) is ever `.collect()`-ed to the driver** —
see `metrics.py`'s Step 8. Everything upstream of that (binning, grouping,
joining, proportion math) stays as unevaluated Spark transformations, so the
same code would scale to a real production score table without a rewrite.

---

## Data flow

```
PostgreSQL classifications table
  → db.read_reference_scores()   — earliest 1000 rows for the active model_version
  → db.read_current_scores()     — last `hours` (default 24) rows, capped at 100k
  → spark.createDataFrame(...)   — Python list → Spark DataFrame (driver-side)
  → metrics.compute_drift()      — binning, PSI, JSD (all Spark ops)
  → db.write_drift_stats()       — one row into drift_stats
  → sys.exit(0 | 1 | 2)          — Airflow's KubernetesPodOperator branches on this
```

Exit codes (`drift_job.py`'s module docstring):
- `0` — ran successfully, no drift
- `1` — configuration or DB error (e.g. `DATABASE_URL` unset, no classifications at all)
- `2` — ran successfully, **drift detected** (PSI > 0.2) — the signal the
  eventual `retrain_dag.py` branches on

---

## PSI and JSD, and why both

**PSI (Population Stability Index)** — the industry-standard metric for "has
this distribution shifted enough to worry about." Sum over bins of
`(p - q) × ln(p / q)`, where `p` is the current proportion and `q` is the
reference proportion in that bin. Thresholds used here (from `metrics.py`'s
docstring, standard in the field):

| PSI | Meaning |
|---|---|
| < 0.10 | No significant change |
| 0.10 – 0.20 | Moderate shift, monitor |
| > 0.20 | Significant drift — triggers retrain |

**JSD (Jensen-Shannon Divergence)** — computed alongside PSI but not
currently used for the go/no-go decision. It's symmetric and bounded
`[0, ln(2)]` even when a bin is empty in one distribution (unlike KL
divergence, which blows up to infinity on a zero-probability bin) — recorded
for visibility into *how* the distribution moved, and as a second signal to
eyeball before trusting a borderline PSI. Both use the same epsilon-smoothed
`p`/`q` (`_EPSILON = 1e-6`) so neither ever divides by zero or takes `ln(0)`.

### The bin-clamping gotcha (`_bin_scores`)

```python
F.greatest(F.least(F.floor(F.col("score") * n_bins).cast("int"), F.lit(n_bins - 1)), F.lit(0))
```

`floor(score * 10)` puts a score of exactly `1.0` in bin `10`, which doesn't
exist (bins are `0`–`9`) — `F.least(..., 9)` clamps the high end. The
**low-end clamp** (`F.greatest(..., 0)`) was added after review: nothing
upstream enforces scores are in `[0, 1]` (no DB `CHECK` constraint on
`classifications.score`), so a floating-point rounding artifact or a future
differently-calibrated model producing a small negative value would silently
fall out of the join in `compute_drift()` instead of landing in bin 0. Clamp
symmetrically rather than trust the input range.

---

## The `get_active_model_version` gotcha (`db.py`)

This is the single most surprising thing in this pipeline, and it went
through two different (both live-tested) implementations before landing on
the current one.

**The trap:** `model_registry` holds rows written by two different
processes that don't share a `model_version` *value* even when they refer to
the same underlying deployment:

1. `services/classifier/db.py`'s `get_active_model()` reads `model_registry`
   to decide *which MinIO artifact to download* — it prefers a row with
   `status='active'`, falling back to `'staging'`.
2. Once a classifier pod has loaded a model, `services/classifier/model.py`
   **self-registers a new row** under its own freshly-derived
   `model_version` string (`sentinel-roberta-{deployed_at}-{quant_tag}`) —
   and *that* string, not the one it downloaded from, is what actually gets
   written into every `classifications.model_version` value going forward.

The first fix mirrored `get_active_model()`'s exact `'active'`-preferred
ordering, on the reasoning that "the drift job should look at whatever's
canonically active." Live-tested against a real cluster, this was wrong: the
`'active'`-status row was a stale promotion pointing at a `model_version`
string from a previous deploy, with **zero** matching rows in
`classifications` — the drift job silently found nothing to compare and
exited before even reaching Spark.

The fix that's actually in the code now drops the status preference
entirely:

```python
SELECT model_version FROM model_registry
WHERE status IN ('active', 'staging')
ORDER BY created_at DESC
LIMIT 1
```

"Whichever pod self-registered most recently" is what's actually running
and writing classifications right now — at the time this was fixed,
nothing reliably kept `status='active'` pointed at the right namespace.
This was a known, accepted gap: once `retrain_dag.py`'s promotion step
landed and started flipping `status` deliberately (rather than every pod
self-registering as `'staging'` on boot), this query should probably go
back to preferring `'active'`. Still subject to the same rolling-restart
race noted in the code comment — old- and new-version pods can both
self-register within moments of each other — just no longer compounded by
a status filter pointing at the wrong `model_version` namespace
altogether.

**Update, now that `retrain_dag.py`'s promotion step exists (Phase 7.3):**
the gap hasn't fully closed on its own. Live-tested during `drift_dag.py`'s
(Phase 7.4) end-to-end verification: `model_registry`'s `active` row
pointed at a `model_version` with **zero** rows in `classifications` —
promoted during retraining-pipeline testing, but never actually served
real traffic, because no classifier pod had been rolled out against it
with real requests flowing yet. `get_active_model_version()`'s
`created_at DESC` (ignoring status) correctly fell back to the most
recently *self-registered* version instead, which is exactly the row with
real data — so the existing fix continues to be the right one even with
promotion logic now in place. The underlying lesson holds either way:
`model_registry.status='active'` answers "which model *should* be
serving," not "which `model_version` string is actually showing up in
`classifications` right now" — and this pipeline specifically needs the
second answer, not the first.

**Lesson for future "obvious" fixes in this codebase:** when a query joins
two tables/processes that evolved independently, verify the *values*
actually line up in a live cluster before trusting that mirroring another
query's logic is correct — matching *shape* isn't the same as matching
*semantics*.

---

## Guardrails added after live testing

- **`MIN_REFERENCE_SIZE = 10`** (`drift_job.py`) — below this many reference
  rows, epsilon-smoothing (`_EPSILON = 1e-6`) dominates the reference
  histogram and PSI/JSD against it aren't statistically meaningful. The job
  now `sys.exit(0)`s (not just logs a warning) rather than risk writing a
  false `drift_flagged=True` — important once Phase 7 wires this exit code
  directly into an unattended retrain trigger; a spurious retrain from an
  unreliable baseline would be expensive and pointless.
- **`MAX_CURRENT_ROWS = 100_000`** (`db.py`) — bounds how much data gets
  pulled into the driver process's memory twice: once via `psycopg`'s
  `fetchall()`, then again via `spark.createDataFrame([(s,) for s in
  scores], ...)`. The query itself is structured as a subquery — `ORDER BY
  ts DESC LIMIT max_rows`, then re-sorted `ASC` in the outer query — so the
  cap keeps the *most recent* rows in the window, not an arbitrary subset
  from the start of it.

---

## Running it locally vs. in-cluster

```bash
# Local, all cores, explicit DB URL
python drift_job.py --hours 24 --database-url postgresql://sentinel:sentinel@localhost:5432/sentinel

# Local, explicit Spark master
python drift_job.py --master local[4]

# In-cluster: spark-on-k8s-operator sets spark.master itself — passing
# --master here would override what the operator configured and break it.
# The SparkApplication CRD (spark-application.yaml) omits --master for
# exactly this reason.
```

`drift_job.py` only calls `.master()` on the `SparkSession.builder` **if**
`--master` was explicitly passed — see the `if args.master:` guard right
before `spark = builder.getOrCreate()`. This is what lets the identical
script run correctly both ways.

---

## `spark-application.yaml` (SparkApplication CRD)

```yaml
image: sentinel-drift:local
imagePullPolicy: Never
mainApplicationFile: local:///opt/spark/work-dir/drift_job.py
sparkVersion: "3.5.3"
driver:
  memory: "512m"
  serviceAccount: spark
executor:
  instances: 2
  memory: "512m"
```

- **`imagePullPolicy: Never`** + `local:///` file scheme — the job's own
  Docker image (built and `k3d image import`-ed by `dev-start.sh`, same
  pattern as classifier/stream-processor) already contains `drift_job.py`,
  `db.py`, `metrics.py`; nothing is pulled from a registry, and
  `mainApplicationFile` points at a path *inside that image*, not a remote
  URL.
- **`serviceAccount: spark`** on the driver — the operator needs this
  identity to create/manage the executor pods it spins up on the driver's
  behalf (RBAC granted by spark-operator's own Helm chart, see
  `infra/terraform/local/explanation.md`'s spark-operator section).
  `restartPolicy: Never` — matches the "one-shot job" semantics; a failed
  drift run should surface as a failure for Airflow to see, not silently
  retry and mask a real problem.
- **`PYSPARK_PYTHON=/usr/bin/python3`** on both driver and executor — the
  `apache/spark-py` base image needs this set explicitly or PySpark can't
  find the interpreter to run `db.py`/`metrics.py` inside the executor
  processes.
- **`arguments: ["--hours", "24", "--reference-size", "1000"]`** — the
  operator passes these straight through as CLI args to
  `mainApplicationFile`, same as running `drift_job.py --hours 24
  --reference-size 1000` locally.

---

## Using `.explain()` to verify the physical plan

`metrics.py`'s `compute_drift()` calls `combined.explain()` right before the
one `.collect()` call. This is the standard way to confirm Spark isn't doing
more work than it needs to — worth running manually when touching this file:

```python
>>> combined.explain()
== Physical Plan ==
*(5) Project [...]
+- *(5) BroadcastHashJoin ...
   :- *(3) HashAggregate(keys=[bin], functions=[count(1)]) ...  # ref_counts
   +- *(4) HashAggregate(keys=[bin], functions=[count(1)]) ...  # cur_counts
```

Two separate `HashAggregate` stages (one per DataFrame) confirms the
reference and current binning genuinely run as independent Spark jobs
rather than accidentally sharing/recomputing state — the thing to check for
whenever this function is refactored is a *redundant full scan* appearing
twice for what should be one source DataFrame, which is the classic
`.explain()`-catchable mistake this project's "Common Interview Points"
section calls out.

---

## Tests

`pipelines/drift/tests/` — run with `pytest pipelines/drift/tests/` from
the `pipelines/drift/` directory (it has its own `pyproject.toml`, separate
from the rest of the repo, since PySpark manages its own environment via
`spark-submit --py-files`, per the root `CLAUDE.md`'s target folder
structure notes). Uses a local `SparkSession` (`local[1]` or similar) to run
`compute_drift()` against small hand-built DataFrames — no live PostgreSQL
or cluster needed for the metrics math itself.
