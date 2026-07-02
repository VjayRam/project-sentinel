"""Quality gate: decide whether a candidate model's benchmark results are
good enough to promote to 'active'.

This does NOT flip model_registry.status itself — per CLAUDE.md's Model
Registry Source of Truth, promotion is Airflow's job (Phase 7, not yet
built). This produces the pass/fail signal + reasons that a human operator
or, later, a retrain DAG acts on: a non-zero exit code should block
promotion the same way drift_job.py's exit code 2 signals "retrain needed."

Usage:
    uv run --package sentinel-evaluation python -m pipelines.evaluation.validate \\
        --candidate logs/evaluation/<run_id>/benchmark_report.json \\
        --baseline  logs/evaluation/<active_run_id>/benchmark_report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

MIN_ACCURACY = 0.85
MAX_ACCURACY_DROP = 0.01  # vs baseline, if a baseline report is given


def validate(candidate: dict, baseline: dict | None = None) -> tuple[bool, list[str]]:
    """Return (passed, reasons). reasons is empty when passed is True."""
    reasons = []

    if candidate["accuracy"] < MIN_ACCURACY:
        reasons.append(
            f"accuracy {candidate['accuracy']:.4f} below minimum {MIN_ACCURACY:.4f}"
        )

    if baseline is not None:
        drop = baseline["accuracy"] - candidate["accuracy"]
        if drop > MAX_ACCURACY_DROP:
            reasons.append(
                f"accuracy dropped {drop:.4f} vs baseline "
                f"({baseline['accuracy']:.4f} -> {candidate['accuracy']:.4f}), "
                f"exceeds max allowed drop {MAX_ACCURACY_DROP:.4f}"
            )

    return len(reasons) == 0, reasons


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gate model promotion on benchmark results")
    p.add_argument("--candidate", required=True, help="Path to the candidate's benchmark_report.json")
    p.add_argument(
        "--baseline",
        default=None,
        help="Path to the current active model's benchmark_report.json (optional)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    candidate_report = json.loads(Path(args.candidate).read_text())
    baseline_report = json.loads(Path(args.baseline).read_text()) if args.baseline else None

    passed, reasons = validate(candidate_report, baseline_report)

    if passed:
        logger.info(
            "PASS — candidate meets quality gate | accuracy=%.4f f1=%.4f",
            candidate_report["accuracy"],
            candidate_report["f1"],
        )
        sys.exit(0)

    logger.error("FAIL — candidate does not meet quality gate:")
    for reason in reasons:
        logger.error("  - %s", reason)
    sys.exit(1)
