# Retraining Pipeline — Explanation

Fine-tunes the content classifier on manually-labelled data, then hands off
to the *existing, unmodified* `pipelines/optimizer` (ONNX export/quantize/
register-as-staging) and `pipelines/evaluation` (quality gate) pipelines —
this package's whole job is producing a good fine-tuned checkpoint and
proving it's good enough, not re-implementing anything downstream of that.
Triggered by `orchestration/retrain_dag.py`, which is triggered by
`services/label-ui`'s "Trigger Retraining" button.

---

## Directory structure

```
pipelines/retraining/
  dataset.py    — build_dataset(): accepted flagged_content + optional CSV sample
  train.py      — fine-tune with transformers.Trainer, log everything to MLflow
  pipeline.py   — orchestrates dataset → train → optimizer → evaluation → report
  __main__.py   — CLI entry point, `python -m pipelines.retraining`
  Dockerfile    — CPU-only torch, repo-root build context (see below)
  pyproject.toml
```

Mirrors `pipelines/optimizer/`'s shape deliberately — `pipeline.py`'s `run()`
returns a `report_path`, same as the optimizer's `run()`; `__main__.py`
exists as a separate file from `pipeline.py` for the same reason the
optimizer split them (`python -m pipelines.retraining`, not
`python -m pipelines.retraining.pipeline`).

---

## `dataset.py` — where the training data actually comes from

```python
def build_dataset(db, initial_dataset_path=None, sample_size=500, seed=0) -> dict:
    accepted = _load_accepted(db)          # flagged_content WHERE training_decision = 'accepted'
    initial = _load_initial_sample(...)    # optional CSV, same shape as datasets/test_dataset.csv
    combined = accepted + initial
    ...
    return {"train": train, "val": val, "sources": {...}}
```

**No initial dataset ships with this repo, and that's deliberate.**
`datasets/test_dataset.csv` is `pipelines/evaluation`'s held-out set — every
retraining run benchmarks the resulting model against it, so sampling from
it here would let the model see (a sample of) the exact data it's later
graded against, inflating the accuracy number the quality gate trusts.
`initial_dataset_path` is optional, `None` by default; when given, it must
be a CSV with the same `raw_text,label` columns `datasets/eval_holdout.py`
already parses — whatever gets dropped in later just works, no new format
to design.

**Plain shuffle + 85/15 split, not stratified** — same reasoning
`datasets/eval_holdout.py` uses for its own sampling: with the input
already reasonably balanced (stream processor's `SAFE_SAMPLE_RATE` keeps it
from skewing all-harm), a random split stays balanced in expectation
without needing explicit stratification logic at this data scale.

**`db.flagged_content.find({"training_decision": "accepted"}, {"input_text": 1, "manual_label": 1})`**
— reads `manual_label`, the human's decision, not `label` (the model's own
classification that flagged the document in the first place). Training on
the model's own predictions would just teach it to keep agreeing with
itself; the whole point of the manual-labelling step
(`services/label-ui`) is a human correcting or confirming that label before
it's trusted as ground truth.

---

## `train.py` — fine-tuning + full MLflow logging

### Always restarts from the base model, never from a previous fine-tune

```python
model = AutoModelForSequenceClassification.from_pretrained(
    base_model_id, num_labels=2, id2label=ID2LABEL, label2id=LABEL2ID,
    ignore_mismatched_sizes=True,
)
```

`model_registry` only ever stores **ONNX artifacts** (for serving), never a
resumable HuggingFace checkpoint — there is nothing to fine-tune *from*
except the original base model. Every retrain therefore trains the base
model fresh on the **full accumulated** accepted-label set, not an
incremental fine-tune-of-a-fine-tune.

**`num_labels=2` + `ignore_mismatched_sizes=True` is a deliberate,
documented simplification.** The base checkpoint's exact original
classification-head shape (a 1-logit sigmoid head vs. a 2-class softmax
head) isn't knowable without a live fetch from the HuggingFace Hub, so this
pins a standard 2-class head and lets HF silently reinitialize it if the
checkpoint's actual head doesn't match. Confirmed live — the console log
for `VijayRam1812/content-classifier-roberta` reads:
```
Some weights of RobertaForSequenceClassification were not initialized from
the model checkpoint ... because the shapes did not match:
- classifier.out_proj.bias: found shape torch.Size([1]) ... torch.Size([2])
```
This means a from-scratch classification head needs enough data and epochs
to learn a good decision boundary — it isn't continuing the original
checkpoint's already-tuned boundary, it's learning a new one. With only a
handful of labelled examples (as in early testing — 40 accepted docs), this
produces a genuinely bad model (50% accuracy, i.e. a coin flip) — which is
exactly what the quality gate downstream is for. See
[`../../orchestration/explanation.md`](../../orchestration/explanation.md)'s
`decide_promotion` section for that gate actually catching this live.

### Dynamic per-batch padding, not fixed-length — the actual OOM fix

```python
class _TextDataset(Dataset):
    def __getitem__(self, idx: int) -> dict:
        enc = self.tokenizer(self.texts[idx], truncation=True, max_length=self.max_length)
        enc["labels"] = self.labels[idx]
        return enc

...
trainer = Trainer(
    ...,
    data_collator=DataCollatorWithPadding(tokenizer),
)
```

The first version of this file padded every example individually to a
fixed `max_length=512` inside `__getitem__` (`padding="max_length"`). This
OOM-killed the retraining pod (`exit_code: 137, reason: OOMKilled`) at
*both* a 4Gi and an 8Gi memory limit, live-reproduced repeatedly. Root
cause: transformer attention cost scales `O(seq_len²)`, and
`flagged_content` spans (chat prompts/responses) are mostly far shorter
than 512 tokens — padding every one of them up to 512 anyway wastes
enormous memory for no accuracy benefit. This is the exact same class of
bug already found and fixed once in `services/classifier/model.py` (see
that file's explanation.md) — it's a natural mistake to reach for `padding=
"max_length"` because it's the simplest thing that works on any single
input, and the memory cost only shows up under batched training/inference.
**The fix is `DataCollatorWithPadding`**: `_TextDataset` returns
variable-length token sequences, and the collator pads each *batch* only to
that batch's own longest example — dramatically less wasted computation for
short-text data.

**No traceback ever appeared for this bug in any log** — `SIGKILL` (what
the kernel OOM-killer sends) gives a process zero chance to flush stdout.
The only way this was actually diagnosed was reading Airflow's *persisted*
task log file, which records the pod's final Kubernetes container status
independent of whatever the container itself managed to print. See
[`../../orchestration/explanation.md`](../../orchestration/explanation.md)'s
debugging-gotcha section for the exact command.

### Per-epoch train *and* eval metrics — HF Trainer only gives you eval for free

```python
class _TrainMetricsCallback(TrainerCallback):
    def on_epoch_end(self, args, state, control, **kwargs):
        trainer = self._trainer_ref[0]
        output = trainer.predict(self.train_dataset, metric_key_prefix="train")
        mlflow.log_metrics({"train_loss": output.metrics["train_loss"], ...}, step=epoch)
```

`Trainer` with `eval_strategy="epoch"` automatically scores `eval_dataset`
every epoch and calls `compute_metrics` on the result — but it never does
this for the *training* set, since re-scoring your own training data isn't
normally something you need. This project explicitly wants both (the user
asked for training loss/accuracy/precision/recall/F1 *and* the same for
eval, at each epoch and at the end), so a `TrainerCallback.on_epoch_end`
runs one extra full forward pass over the train split each epoch —
`metric_key_prefix="train"` makes `predict()`'s own internal
`compute_metrics` call come back with keys already named `train_loss`,
`train_accuracy`, etc., instead of the default `test_*` prefix.

**`trainer_ref` is a one-element list, not a direct reference** — the
callback object has to be constructed *before* passing it into
`Trainer(callbacks=[...])`, but it needs to call methods on that same
`Trainer` once training starts. A mutable one-element list is populated
with the real `Trainer` instance immediately after construction
(`trainer_ref[0] = trainer`), giving the callback a way to reach an object
that didn't exist yet when the callback itself was built.

### No scikit-learn — confusion-matrix arithmetic instead

```python
def _metrics_from_confusion(tp, tn, fp, fn) -> dict:
    accuracy  = (tp + tn) / max(tp + tn + fp + fn, 1)
    precision = tp / max(tp + fp, 1)
    recall    = tp / max(tp + fn, 1)
    f1        = 2 * precision * recall / max(precision + recall, 1e-9)
```

Matches `pipelines/evaluation/benchmark.py`'s existing no-sklearn
convention (that file computes AUC-ROC via a rank-based Mann-Whitney U
formula for the same reason) — a handful of confusion-matrix counts don't
justify pulling in a second, heavier metrics dependency alongside torch/
transformers/mlflow, which this package already needs regardless.

### `report_to=[]` + `disable_tqdm=True` — logging is manual, and quiet

```python
args = TrainingArguments(
    ...,
    report_to=[],          # avoid double-logging via Trainer's own MLflow auto-integration
    disable_tqdm=True,
)
```

`report_to=[]` disables `Trainer`'s own built-in MLflow callback — without
it, `Trainer` would log its own version of these metrics to whatever MLflow
run happens to be active, duplicating (and potentially conflicting with)
the explicit `mlflow.log_metrics()` calls this file already makes.
`disable_tqdm=True` was added chasing what looked like a `kubectl logs -f`
streaming issue (tqdm's `\r`-based progress bar seemed like a plausible
cause of "new lines stop appearing"); it turned out the real cause both
before and after this change was the OOM kill above, not tqdm — but
disabling a meaningless progress bar in a headless pod's logs is a
reasonable thing to do regardless, so it stayed.

### What doesn't get logged to MLflow, and why

```python
checkpoint_dir = output_dir / "checkpoint"
trainer.save_model(str(checkpoint_dir))
tokenizer.save_pretrained(str(checkpoint_dir))
mlflow.set_tag("checkpoint_path", str(checkpoint_dir))  # not mlflow.log_artifact(...)
```

The fine-tuned checkpoint is saved to local disk and referenced by a plain
MLflow **tag** (a string), not uploaded as an MLflow **artifact**. MinIO
already becomes the artifact store of record once `pipelines/optimizer`
uploads the ONNX conversion under `model_registry.model_path` — logging the
same model's weights a second time (as a raw PyTorch checkpoint, in
MLflow's own S3 artifact store) would mean two systems of record for what
is conceptually one model version.

---

## `pipeline.py` — orchestration, reusing everything downstream unchanged

```python
mlflow.set_experiment("sentinel-retraining")
with mlflow.start_run(run_name=run_id) as mlflow_run:
    train_result = train(dataset, base_model_id, run_artifacts / "finetuned", epochs=epochs)
    checkpoint_dir = train_result.pop("checkpoint_dir")

optimizer_report_path = run_optimizer(model_id=str(checkpoint_dir), output_dir=output_dir, log_dir=log_dir)
...
benchmark_report = run_benchmark(model_dir=str(int8_dir))
gate_passed, reasons = validate(benchmark_report)
```

**The fine-tuned checkpoint plugs into `pipelines/optimizer` with zero
changes to that package.** `run_optimizer(model_id=...)` passes `model_id`
straight through to `optimum.exporters.onnx.main_export()`, which accepts a
local directory path exactly as readily as a HuggingFace Hub id — the
export/optimize/quantize/upload/register chain has no idea (and doesn't
need to know) whether `model_id` came from the Hub or from a fine-tuning
run five seconds ago in the same process.

**Same pattern for `pipelines/evaluation`** — `run_benchmark(model_dir=...)`
and `validate(...)` are plain importable functions, called directly rather
than shelled out to a subprocess, exactly the way `pipelines/optimizer/
pipeline.py` calls its own stage functions (`export()`, `optimize()`,
`quantize()`) as direct Python calls.

**Registers as `'staging'`, never `'active'`** — `run_optimizer`'s own
`registry.py` always inserts new rows as `'staging'` (see that package's
explanation.md); this pipeline doesn't touch that behavior. Promotion to
`'active'` happens exactly once, in `orchestration/retrain_dag.py`'s
`decide_promotion` task, and only if `gate_passed` is `True`.

### The XCom handoff — no Airflow import in this package

```python
xcom_dir = Path("/airflow/xcom")
if xcom_dir.exists():
    (xcom_dir / "return.json").write_text(json.dumps(report))
```

`orchestration/retrain_dag.py`'s `KubernetesPodOperator` (`do_xcom_push=True`)
mounts a sidecar container at `/airflow/xcom` and tails whatever gets
written to `return.json` there, making it available to downstream tasks via
Airflow's normal XCom mechanism. This package doesn't import anything
Airflow-specific to participate in that — it just checks whether that path
exists and writes to it if so. Running `python -m pipelines.retraining`
directly from a shell (no Airflow involved at all) skips this block
entirely and works exactly the same otherwise; the DAG is a caller, not a
dependency.

---

## `Dockerfile` — CPU-only torch, repo-root build context

```dockerfile
# Build context MUST be the repo root, not this directory:
#   docker build -f pipelines/retraining/Dockerfile -t sentinel-retraining:local .
FROM python:3.12-slim
...
RUN uv pip install --python /app/.venv/bin/python \
    --index-url https://download.pytorch.org/whl/cpu "torch>=2.2" && \
    uv pip install --python /app/.venv/bin/python \
    "transformers>=4.48" "accelerate>=0.26.0" "mlflow>=2.19" ...
COPY pipelines /app/pipelines
COPY datasets /app/datasets
```

**Repo-root build context, unlike every other Dockerfile in this repo.**
`pipeline.py` imports `pipelines.optimizer`, `pipelines.evaluation`, and
(transitively, via `benchmark.py`) `datasets.eval_holdout` directly — all
three trees need to be present in the image, not just this package's own
files. `drift/`'s Dockerfile is self-contained by contrast because
`drift_job.py` doesn't import across package boundaries the same way.

**CPU-only torch wheel, installed explicitly via `--index-url .../whl/cpu`**
— this pod runs inside k3d, which has no GPU passthrough configured (real
production systems run retraining on separately-provisioned,
training-suitable compute — a dedicated GPU node pool or a managed training
service — rather than sharing a serving cluster's resources; adding that
here would be new infra beyond this project's current phase). Installing
the default (CUDA) wheel would pull in several GB of CUDA runtime this pod
can never use.

**`accelerate>=0.26.0` is a hard requirement, not optional** —
`transformers.Trainer` raises `ImportError: Using the Trainer with PyTorch
requires accelerate>=0.26.0` at the moment `Trainer(...)` is constructed if
it's missing. Easy to miss when writing the code locally against an
environment that already has it installed as some other package's
dependency; only surfaced once this ran inside the minimal container image.

---

## Live-tuned pod resources (set in `orchestration/retrain_dag.py`, not here)

This package has no opinion on how much CPU/memory it gets — that's the
DAG's `KubernetesPodOperator.container_resources`. Worth knowing when
debugging a failure in *this* code that's actually a resource-limit
problem one layer up: see
[`../../orchestration/explanation.md`](../../orchestration/explanation.md)'s
`run_retraining` section for the full three-round tuning story (memory
OOM → the padding fix above → a separate CPU-limit issue that made
`pipelines/evaluation/benchmark.py`'s ONNX Runtime session thrash against
too few cores).

---

## Environment variables / CLI arguments

| Variable / flag | Default | Effect |
|---|---|---|
| `MONGO_URI` / `--mongo-uri` | `mongodb://sentinel:sentinel@localhost:27017/sentinel` | Source of accepted `flagged_content` |
| `BASE_MODEL_ID` / `--base-model-id` | `VijayRam1812/content-classifier-roberta` | HF Hub id to fine-tune from |
| `--output-dir` | *(required)* | Where checkpoints + ONNX artifacts land |
| `--log-dir` | `logs` | Where `report.json` lands |
| `INITIAL_DATASET_PATH` / `--initial-dataset-path` | `None` | Optional CSV sample, see `dataset.py` above |
| `--sample-size` | `500` | Max rows drawn from the initial CSV, if given |
| `--epochs` | `3` | Fine-tuning epochs |
| `DATABASE_URL` | *(read by `pipelines.optimizer.registry`)* | Where the new `model_registry` row gets inserted |
| `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` | *(read by `pipelines.optimizer.upload`)* | Where the ONNX artifacts get uploaded |
| `MLFLOW_TRACKING_URI` | *(read by the `mlflow` client itself)* | Where the fine-tuning run gets logged |

---

## Tips and tricks

**Run the whole pipeline locally, outside Airflow entirely** (useful for
iterating on `train.py` without a K8s pod round-trip each time):
```bash
uv run --package sentinel-retraining python -m pipelines.retraining \
  --output-dir artifacts --log-dir logs --epochs 1
```

**Check a run's full report without digging through MLflow's UI:**
```bash
cat logs/retraining/<run_id>/report.json | python3 -m json.tool
```

**Confirm the dynamic-padding fix is actually in effect** (i.e. you're not
looking at a stale image): a fixed-`max_length` tokenizer call pads every
batch to the same shape regardless of content, so a quick way to check
without reading source is to compare wall-clock time for a tiny 1-epoch run
before/after — dynamic padding should be visibly faster on short text.

**Watch a live run's logs without the `kubectl logs -f` streaming issue**
described above (and in `orchestration/explanation.md`) — prefer polling
snapshots over `-f`:
```bash
watch -n 5 'kubectl logs -n sentinel-pipeline <pod> -c base --tail=20'
```
