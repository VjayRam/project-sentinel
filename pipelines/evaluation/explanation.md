# Evaluation Pipeline — Explanation

The model quality gate: `benchmark.py` scores a candidate ONNX model against
a held-out ground-truth dataset and writes accuracy/precision/recall/F1/
AUC-ROC + latency/memory numbers to a JSON report; `validate.py` reads that
report (optionally alongside the currently-active model's report) and
decides pass/fail via a non-zero exit code. Both are one-shot CLI scripts —
eventually a K8s `Job` step in the retrain DAG, run locally today.

Neither script talks to `model_registry` or flips any status. Per
`CLAUDE.md`'s "Model Registry Source of Truth," promotion is exclusively
Airflow's job (Phase 7, not yet built) — this pipeline only produces the
signal that a human operator, or later `retrain_dag.py`, acts on.

---

## Data flow

```
pipelines/optimizer output (a quantized ONNX model dir)
  → benchmark.py --model-dir logs/optimizer/<run_id>/int8
      → datasets.eval_holdout.load_holdout()   — 3780 labeled examples
      → ONNX Runtime inference, batched
      → benchmark_report.json (accuracy, F1, AUC-ROC, latency, memory)
  → validate.py --candidate <report> [--baseline <active model's report>]
      → exit 0 (PASS) or exit 1 (FAIL), reasons logged
```

Run both from the repo root with `uv run --package sentinel-evaluation`
(see `pyproject.toml` in this directory) — a separate package from the rest
of `pipelines/`, same reasoning as `pipelines/drift/`'s own `pyproject.toml`:
each pipeline step is meant to become an independently-buildable container.

---

## The held-out dataset (`datasets/eval_holdout.py`)

`datasets/test_dataset.csv` — 3780 rows, sourced from
`github.com/VjayRam/Content-Identifier`, balanced across 9 risk categories
(VC, DEF, ESP, PII, SHS, IP, CBRN, CSAE, SCAM) at 210 harmful + 210 safe
examples each. `label=1` means the row's text matches its risk category
(harmful); `label=0` is a safe or counterfactual example for the same
category. This is strictly an **evaluation** set — never used for training,
which is what makes it valid as a promotion gate (a model that was fine-tuned
on data leaking into this set would look artificially good here).

`csv.field_size_limit(10_000_000)` exists because several rows are
multi-turn conversations stored as one multiline CSV field — comfortably
past Python's 128KB default field-size limit, which raises
`_csv.Error: field larger than field limit` on the first oversized row
without it. `load_holdout(sample_size=...)` draws a plain random sample
(no explicit stratification) for fast local iteration; since the source set
is already exactly balanced 50/50, an unstratified sample stays balanced
in expectation without needing separate per-class sampling logic.

---

## `benchmark.py`

### Scoring mirrors the classifier, deliberately not by importing it

`_score_batch()`'s sigmoid-for-single-logit / softmax-last-class branching
is a copy of `services/classifier/model.py`'s `Classifier.predict()` scoring
logic — **kept standalone rather than imported**. `pipelines/` and
`services/` are separate deployable packages by design (see `CLAUDE.md`'s
target production folder structure — each becomes its own container with
its own `Dockerfile`), so sharing runtime code between them would mean
either a shared internal package (not worth it yet at this scale) or an ugly
cross-package import reaching into another service's source tree. Small
enough duplication (a dozen lines) that keeping it standalone is the
pragmatic call — but it does mean: **if the classifier's scoring logic ever
changes (e.g. a different activation, a different logit convention), this
function has to be updated to match by hand.** Nothing enforces the two
stay in sync.

### AUC-ROC without scikit-learn

```python
def _auc_roc(scores, labels) -> float | None:
    combined = np.concatenate([pos, neg])
    order = np.argsort(combined)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(combined) + 1)
    pos_rank_sum = ranks[: len(pos)].sum()
    auc = (pos_rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))
```

This is the Mann-Whitney U statistic formulation of AUC-ROC: rank every
score across both classes, sum the ranks belonging to the positive class,
subtract the minimum possible sum, normalize by the number of
positive/negative pairs. Avoids pulling in scikit-learn as a dependency for
one metric. The `argsort`-of-`argsort` trick (`order = argsort(combined)`,
then scatter `1..N` back into rank-order via `ranks[order] = ...`) gives
exact ranks for distinct scores; under ties it approximates (ties should get
the *average* rank, this gives each tied element its own consecutive rank
instead) — acceptable for a promotion gate where "is this candidate roughly
as good," not a research benchmark needing exact tie handling. Returns
`None` (not `0.0` or an exception) when a class is entirely absent from the
sample — `validate.py` never looks at `auc_roc` for pass/fail today, so this
mainly guards against a `ZeroDivisionError` on a pathological
`--sample-size` draw.

### `peak_memory_mb` via `resource.getrusage`

```python
peak_memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
```

`ru_maxrss` is the process's peak resident-set size **since the process
started**, not a windowed measurement around just the inference loop — it
includes interpreter startup, `onnxruntime`/`transformers` import overhead,
and model loading. Good enough for the gate's actual question ("did this
candidate get dramatically heavier than the baseline"), not a substitute for
a real profiler if you need to know where memory goes. Units: `ru_maxrss` is
**kilobytes on Linux** (the only platform this runs on) — macOS reports
bytes instead, which would need `/1024/1024` rather than `/1024`; the `/
1024` here would silently be wrong by 1000x if this ever ran on a Mac.

### Batching

`BATCH_SIZE = 32`, plain Python list slicing (`texts[i:i+BATCH_SIZE]`) —
one ONNX Runtime `session.run()` call per batch, results concatenated with
`np.concatenate`. No async, no `DynamicBatcher` (that's the classifier
service's concern for handling concurrent live requests) — this is a
sequential offline batch job with no concurrency to coalesce.

---

## `validate.py`

```python
MIN_ACCURACY = 0.85
MAX_ACCURACY_DROP = 0.01  # vs baseline, if a baseline report is given
```

Two independent gates, both must pass:

1. **Absolute floor** — candidate accuracy must be ≥ 0.85, regardless of
   what came before. Catches a candidate that's just bad in isolation (e.g.
   a broken quantization pass, a corrupted checkpoint).
2. **Regression guard** — *only checked if `--baseline` is passed* — the
   candidate can't be more than 1 percentage point worse than the currently
   active model. Catches a candidate that clears the absolute floor but is
   still a meaningful step backward (e.g. retraining on a skewed new data
   slice that improves one risk category at the expense of others).

`--baseline` is optional by design: the very first model promoted ever has
no baseline to compare against, and the absolute floor alone is the gate in
that case. `validate()` (the pure function, no I/O) returns `(passed: bool,
reasons: list[str])` rather than just a bool — `reasons` is what gets logged
on failure and is exactly what a human (or eventually a Slack/GitHub-issue
notification from the retrain DAG) needs to see to understand *why* a
promotion was blocked, not just that it was.

Exit codes mirror `pipelines/drift/drift_job.py`'s convention deliberately:
`0` = proceed, non-zero = don't. This is what lets `retrain_dag.py`
branch on both pipeline steps the same way (`BranchPythonOperator` /
`ShortCircuitOperator` checking a `KubernetesPodOperator`'s exit code)
without needing per-step-specific logic.

---

## Running it locally

```bash
# 1. Benchmark a candidate produced by the optimizer pipeline
uv run --package sentinel-evaluation python -m pipelines.evaluation.benchmark \
  --model-dir logs/optimizer/<run_id>/int8 \
  --output logs/evaluation/<run_id>/benchmark_report.json

# 2. Gate it against the floor only
uv run --package sentinel-evaluation python -m pipelines.evaluation.validate \
  --candidate logs/evaluation/<run_id>/benchmark_report.json

# 2b. Or gate it against the currently-active model's own report too
uv run --package sentinel-evaluation python -m pipelines.evaluation.validate \
  --candidate logs/evaluation/<run_id>/benchmark_report.json \
  --baseline  logs/evaluation/<active_run_id>/benchmark_report.json

echo $?   # 0 = PASS, 1 = FAIL
```

`--sample-size` on `benchmark.py` is useful for a fast sanity check during
iteration (a few hundred examples instead of all 3780) — don't use a
sampled run's report as the actual promotion-gate input; the full 3780-row
set is what the 0.85 floor and 1%-drop tolerance were calibrated against.

---

## What's next (Phase 7.3)

Once `retrain_dag.py` exists, this becomes two `KubernetesPodOperator` tasks
(benchmark, then validate) sandwiched between the optimizer pipeline and the
promotion step — see [`../../orchestration/explanation.md`](../../orchestration/explanation.md)'s
"What's next" section for the full intended DAG shape.
