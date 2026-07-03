"""Loader for the evaluation pipeline's held-out ground-truth set.

test_dataset.csv (3780 rows, from github.com/VjayRam/Content-Identifier) is
balanced across 9 risk categories (VC, DEF, ESP, PII, SHS, IP, CBRN, CSAE,
SCAM), 210 harmful + 210 safe examples each. label=1 marks content matching
the row's risk category; label=0 is a safe/counterfactual example. Used only
to compute accuracy/F1/AUC-ROC for a candidate model before it's considered
for promotion (pipelines/evaluation) — not a training set.
"""

import csv
import random
from pathlib import Path

# Multi-turn conversations are stored as one multi-line CSV field — well
# past csv's 128KB default field size limit for the largest rows.
csv.field_size_limit(10_000_000)

_CSV_PATH = Path(__file__).parent / "test_dataset.csv"
_LABEL_MAP = {"1": "harm", "0": "safe"}


def load_holdout(sample_size: int | None = None, seed: int = 0) -> list[tuple[str, str]]:
    """Return (text, label) pairs, label in {"safe", "harm"}.

    sample_size, if given, draws a stratified-by-nothing random sample (the
    source set is already balanced 50/50, so a plain random sample stays
    balanced in expectation) — useful for fast local iteration against the
    full 3780-row set.
    """
    with _CSV_PATH.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pairs = [(r["raw_text"], _LABEL_MAP[r["label"]]) for r in rows]

    if sample_size is not None and sample_size < len(pairs):
        pairs = random.Random(seed).sample(pairs, sample_size)

    return pairs
