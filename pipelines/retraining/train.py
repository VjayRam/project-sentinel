"""Fine-tunes the content classifier and logs the run to MLflow: dataset
sources/sizes, training config, and per-epoch + final train/eval loss,
accuracy, precision, recall, F1.

Precision/recall/F1 are computed from confusion-matrix counts, not
scikit-learn — matches pipelines/evaluation/benchmark.py's existing
no-sklearn convention (this package doesn't want a second, heavier metrics
dependency when tp/fp/fn arithmetic is a few lines).
"""

import logging
import random
import time
from pathlib import Path

import mlflow
import numpy as np
from torch.utils.data import Dataset, Subset
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    DataCollatorWithPadding,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

logger = logging.getLogger(__name__)

ID2LABEL = {0: "safe", 1: "harm"}
LABEL2ID = {"safe": 0, "harm": 1}

# _TrainMetricsCallback's per-epoch forward pass over the train split is
# pure logging overhead, not part of the actual training step — its cost
# must not scale unbounded with the training set size. This was part of
# the root cause of an earlier OOM (see orchestration/retrain_dag.py's
# container_resources comment); the real fix was dynamic per-batch padding
# (this file's DataCollatorWithPadding), but capping the sample size here
# bounds the remaining overhead regardless of how large the accepted-label
# set grows in the future.
MAX_TRAIN_METRICS_SAMPLES = 200


class _TextDataset(Dataset):
    def __init__(self, pairs: list[tuple[str, str]], tokenizer, max_length: int = 512):
        self.texts = [t for t, _ in pairs]
        self.labels = [LABEL2ID[label] for _, label in pairs]
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.texts)

    def __getitem__(self, idx: int) -> dict:
        # No padding here — DataCollatorWithPadding pads per-batch to the
        # longest example in that batch, not a fixed max_length. Padding
        # every example to 512 tokens individually (the previous approach)
        # OOM-killed the pod at both 4Gi and 8Gi limits (live-reproduced,
        # exit_code 137/OOMKilled) — flagged_content spans are mostly much
        # shorter than 512 tokens, so uniform max-length padding wastes
        # enormous memory on attention's O(seq_len^2) scaling for no reason.
        # Same class of bug already documented and fixed once in
        # services/classifier/model.py — see that file's explanation.md.
        enc = self.tokenizer(self.texts[idx], truncation=True, max_length=self.max_length)
        enc["labels"] = self.labels[idx]
        return enc


def _metrics_from_confusion(tp: int, tn: int, fp: int, fn: int) -> dict:
    accuracy = (tp + tn) / max(tp + tn + fp + fn, 1)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1}


def _compute_metrics(eval_pred) -> dict:
    logits, labels = eval_pred
    preds = np.argmax(logits, axis=-1)
    labels = np.array(labels)
    tp = int(((preds == 1) & (labels == 1)).sum())
    tn = int(((preds == 0) & (labels == 0)).sum())
    fp = int(((preds == 1) & (labels == 0)).sum())
    fn = int(((preds == 0) & (labels == 1)).sum())
    return _metrics_from_confusion(tp, tn, fp, fn)


class _TrainMetricsCallback(TrainerCallback):
    """HF Trainer only auto-evaluates eval_dataset each epoch — this scores
    the train split too (metric_key_prefix="train" so Trainer's own
    compute_metrics call comes back pre-labelled train_accuracy/train_f1/...),
    so both train_* and eval_* land in MLflow for every epoch.

    Scores a fixed subset of at most MAX_TRAIN_METRICS_SAMPLES, not the
    full train split — this is a diagnostic forward pass with no bearing
    on the actual training step, and its cost shouldn't grow unbounded
    with the training set. The same fixed subset (seeded, chosen once at
    construction) is reused every epoch so metric trends across epochs are
    comparable against a consistent sample rather than a new random draw
    each time.

    trainer_ref is a one-element list populated after Trainer construction —
    the callback has to be built before the Trainer it references exists.
    """

    def __init__(self, trainer_ref: list, train_dataset):
        self._trainer_ref = trainer_ref
        if len(train_dataset) > MAX_TRAIN_METRICS_SAMPLES:
            indices = random.Random(0).sample(range(len(train_dataset)), MAX_TRAIN_METRICS_SAMPLES)
            self.train_dataset = Subset(train_dataset, indices)
        else:
            self.train_dataset = train_dataset

    def on_epoch_end(self, args, state, control, **kwargs):
        trainer = self._trainer_ref[0]
        output = trainer.predict(self.train_dataset, metric_key_prefix="train")
        epoch = int(state.epoch) if state.epoch is not None else 0
        metrics = {
            "train_loss": output.metrics["train_loss"],
            "train_accuracy": output.metrics["train_accuracy"],
            "train_precision": output.metrics["train_precision"],
            "train_recall": output.metrics["train_recall"],
            "train_f1": output.metrics["train_f1"],
        }
        mlflow.log_metrics(metrics, step=epoch)
        logger.info("Epoch %d train metrics: %s", epoch, metrics)


def train(
    dataset: dict,
    base_model_id: str,
    output_dir: Path,
    epochs: int = 3,
    batch_size: int = 8,
    learning_rate: float = 2e-5,
) -> dict:
    """Fine-tunes base_model_id on dataset['train'], evaluates on
    dataset['val'] each epoch, logs everything to the caller's active MLflow
    run (caller owns mlflow.start_run()), and saves the checkpoint to
    output_dir. Returns final eval metrics plus checkpoint_dir.

    Always restarts from base_model_id rather than a previous fine-tune —
    model_registry only stores ONNX artifacts (not resumable HF
    checkpoints), so there is nothing to resume from; every retrain fine-
    tunes the base model on the full accumulated accepted-label set instead.
    num_labels=2 + ignore_mismatched_sizes=True: the base checkpoint's exact
    original head shape (1-logit sigmoid vs 2-class softmax) isn't known
    without a live hub fetch, so this pins a standard 2-class head and lets
    HF reinitialize it if the base checkpoint's head doesn't already match —
    a deliberate simplification for this phase, noted here since it means a
    from-scratch head needs enough data/epochs to learn a good boundary
    rather than continuing the original checkpoint's exact decision boundary.
    """
    tokenizer = AutoTokenizer.from_pretrained(base_model_id)
    model = AutoModelForSequenceClassification.from_pretrained(
        base_model_id,
        num_labels=2,
        id2label=ID2LABEL,
        label2id=LABEL2ID,
        ignore_mismatched_sizes=True,
    )

    train_ds = _TextDataset(dataset["train"], tokenizer)
    val_ds = _TextDataset(dataset["val"], tokenizer)

    mlflow.log_params(
        {
            "base_model_id": base_model_id,
            "epochs": epochs,
            "batch_size": batch_size,
            "learning_rate": learning_rate,
            "train_size": len(train_ds),
            "val_size": len(val_ds),
            **{f"source_{k}": v for k, v in dataset["sources"].items()},
        }
    )

    args = TrainingArguments(
        output_dir=str(output_dir / "checkpoints"),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        learning_rate=learning_rate,
        eval_strategy="epoch",
        logging_strategy="epoch",
        save_strategy="no",  # final checkpoint saved explicitly below
        report_to=[],  # logging to MLflow is manual — avoid double-logging via Trainer's own auto-integration
        # tqdm's progress bar redraws a single line via \r rather than
        # emitting newlines — in a headless pod, that confuses line-based
        # log streaming (kubectl logs -f appeared to "freeze" mid-training
        # with no new lines, live-reproduced) and is meaningless with no
        # terminal to render it in anyway.
        disable_tqdm=True,
    )

    trainer_ref: list = [None]
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        # Pads each batch to its own longest example, not a fixed 512 —
        # pairs with _TextDataset no longer padding in __getitem__.
        data_collator=DataCollatorWithPadding(tokenizer),
        compute_metrics=_compute_metrics,
        callbacks=[_TrainMetricsCallback(trainer_ref, train_ds)],
    )
    trainer_ref[0] = trainer

    t0 = time.perf_counter()
    trainer.train()
    training_time_s = time.perf_counter() - t0

    # report_to=[] disabled Trainer's own MLflow auto-integration, so the
    # per-epoch eval history (loss + compute_metrics output, "eval_"
    # prefixed) is logged explicitly here from log_history instead.
    eval_entries = [e for e in trainer.state.log_history if "eval_loss" in e]
    for entry in eval_entries:
        epoch = int(entry.get("epoch", 0))
        mlflow.log_metrics(
            {
                "eval_loss": entry["eval_loss"],
                "eval_accuracy": entry.get("eval_accuracy", 0.0),
                "eval_precision": entry.get("eval_precision", 0.0),
                "eval_recall": entry.get("eval_recall", 0.0),
                "eval_f1": entry.get("eval_f1", 0.0),
            },
            step=epoch,
        )

    final_eval = eval_entries[-1] if eval_entries else {}
    final_metrics = {
        "eval_loss": final_eval.get("eval_loss", 0.0),
        "eval_accuracy": final_eval.get("eval_accuracy", 0.0),
        "eval_precision": final_eval.get("eval_precision", 0.0),
        "eval_recall": final_eval.get("eval_recall", 0.0),
        "eval_f1": final_eval.get("eval_f1", 0.0),
        "training_time_s": training_time_s,
    }
    mlflow.log_metrics(final_metrics)  # no step= — this is the run's summary metric set

    checkpoint_dir = output_dir / "checkpoint"
    trainer.save_model(str(checkpoint_dir))
    tokenizer.save_pretrained(str(checkpoint_dir))
    # Not logged as an MLflow artifact — that would duplicate MinIO's role
    # once the optimizer pipeline uploads the ONNX conversion. A tag keeps
    # the run traceable back to the checkpoint without storing it twice.
    mlflow.set_tag("checkpoint_path", str(checkpoint_dir))

    logger.info("Training complete | %.1fs | final=%s", training_time_s, final_metrics)
    return {"checkpoint_dir": checkpoint_dir, **final_metrics}
