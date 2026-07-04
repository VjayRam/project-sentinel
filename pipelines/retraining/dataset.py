"""Builds the fine-tuning dataset from manually-accepted flagged_content,
plus an optional sample of an external labelled CSV.

No initial dataset file ships with this repo — datasets/test_dataset.csv is
the evaluation pipeline's held-out set and must never be sampled from here
(that would contaminate the accuracy numbers pipelines/evaluation gates
promotion on). initial_dataset_path is therefore optional and unset by
default; when provided, it must be a CSV with the same raw_text,label shape
datasets/eval_holdout.py already parses, so whatever gets dropped in later
works with no format guessing.
"""

import csv
import logging
import random
from pathlib import Path

import pymongo

logger = logging.getLogger(__name__)

# Multi-turn conversations can be one multi-line CSV field — well past csv's
# 128KB default field size limit. Mirrors datasets/eval_holdout.py.
csv.field_size_limit(10_000_000)

_LABEL_MAP = {"1": "harm", "0": "safe"}
VAL_FRACTION = 0.15


def _load_accepted(db: pymongo.database.Database) -> list[tuple[str, str]]:
    cursor = db.flagged_content.find(
        {"training_decision": "accepted"},
        {"input_text": 1, "manual_label": 1},
    )
    return [(d["input_text"], d["manual_label"]) for d in cursor if d.get("manual_label")]


def _load_initial_sample(path: str, sample_size: int, seed: int) -> list[tuple[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pairs = [(r["raw_text"], _LABEL_MAP[r["label"]]) for r in rows]
    if sample_size < len(pairs):
        pairs = random.Random(seed).sample(pairs, sample_size)
    return pairs


def build_dataset(
    db: pymongo.database.Database,
    initial_dataset_path: str | None = None,
    sample_size: int = 500,
    seed: int = 0,
) -> dict:
    """Returns {"train": [(text,label),...], "val": [...], "sources": {...}}.

    Plain shuffle + split, not stratified — same reasoning as
    eval_holdout.py's sampling: with class balance already reasonable in
    expectation (flagged_content's SAFE_SAMPLE_RATE keeps it from skewing
    all-harm), a random split stays balanced in expectation without needing
    explicit stratification logic for a dataset this small.
    """
    accepted = _load_accepted(db)

    initial: list[tuple[str, str]] = []
    if initial_dataset_path and Path(initial_dataset_path).exists():
        initial = _load_initial_sample(initial_dataset_path, sample_size, seed)
    elif initial_dataset_path:
        logger.warning("initial_dataset_path=%s does not exist — skipping", initial_dataset_path)

    combined = accepted + initial
    # Need at least 2 — one for training, one for validation. With exactly
    # 1, n_val's max(1, ...) below would take that single example for val
    # and leave train empty, silently constructing a Trainer with a
    # zero-length train_dataset instead of failing loudly.
    if len(combined) < 2:
        raise ValueError(
            f"Only {len(combined)} labelled example(s) available — need at least 2 "
            "(one for training, one for validation). Accept more flagged_content in "
            "the labelling UI first, or pass --initial-dataset-path."
        )

    rng = random.Random(seed)
    rng.shuffle(combined)
    n_val = max(1, int(len(combined) * VAL_FRACTION))
    val, train = combined[:n_val], combined[n_val:]

    logger.info(
        "Dataset built | accepted=%d initial_sample=%d train=%d val=%d",
        len(accepted),
        len(initial),
        len(train),
        len(val),
    )
    return {
        "train": train,
        "val": val,
        "sources": {
            "flagged_content_accepted": len(accepted),
            "initial_dataset_sample": len(initial),
        },
    }
