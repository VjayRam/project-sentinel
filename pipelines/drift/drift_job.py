"""
Sentinel drift detection job — PySpark edition.

All binning and metric computation (PSI, JSD) runs in Spark. Scores are read
from PostgreSQL via psycopg and converted to Spark DataFrames so the job works
without a JDBC driver in local dev.

Run modes:
  Local (all cores):
    python drift_job.py [--hours 24] [--database-url postgresql://...]

  Explicit Spark master:
    python drift_job.py --master local[4]
    python drift_job.py --master spark://host:7077

  spark-submit (K8s operator, Phase 7):
    spark-submit --py-files metrics.py,db.py drift_job.py --hours 24 --master k8s://...

Exit codes:
  0 — ran successfully, no drift detected
  1 — configuration or DB error
  2 — ran successfully, drift detected (PSI > 0.2)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Below this many reference rows, epsilon-smoothing dominates the reference
# histogram and PSI/JSD against it are not statistically meaningful.
MIN_REFERENCE_SIZE = 10


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sentinel drift detection job")
    p.add_argument("--hours", type=int, default=24, help="Current window size in hours")
    p.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN (or set DATABASE_URL env var)",
    )
    p.add_argument(
        "--reference-size",
        type=int,
        default=1000,
        help="Number of earliest rows to use as the reference baseline",
    )
    p.add_argument(
        "--master",
        default=None,
        help=(
            "Spark master URL for local dev (e.g. local[*]). "
            "Omit when running via spark-on-k8s-operator — the operator sets the master."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.database_url:
        logger.error("DATABASE_URL not set — pass --database-url or set the env var")
        sys.exit(1)

    from db import (
        get_active_model_version,
        read_current_scores,
        read_reference_scores,
        write_drift_stats,
    )
    from metrics import compute_drift

    # ── resolve model version ─────────────────────────────────────────────────
    model_version = get_active_model_version(args.database_url)
    if not model_version:
        logger.error("No classifications in DB yet — nothing to evaluate drift for")
        sys.exit(1)

    logger.info(
        "Drift job starting | model=%s | window=%dh | master=%s",
        model_version,
        args.hours,
        args.master or "operator-managed",
    )

    # ── read scores from PostgreSQL ───────────────────────────────────────────
    ref_scores = read_reference_scores(args.database_url, model_version, size=args.reference_size)
    if len(ref_scores) < MIN_REFERENCE_SIZE:
        # Too few rows to build a meaningful reference distribution — with this
        # little data, epsilon-smoothing dominates the reference histogram and
        # PSI/JSD against it are not trustworthy. Skip rather than risk writing
        # a false drift_flagged=True (which would fire an unwarranted retrain
        # once this is wired into Phase 7 automation).
        logger.warning(
            "Only %d reference rows for model %s — need at least %d for reliable "
            "PSI; skipping this run rather than compute drift against an unreliable "
            "baseline",
            len(ref_scores),
            model_version,
            MIN_REFERENCE_SIZE,
        )
        sys.exit(0)

    cur_scores, window_start, window_end = read_current_scores(
        args.database_url, model_version, hours=args.hours
    )
    if not cur_scores:
        logger.warning("No current-window rows found — skipping drift computation")
        sys.exit(0)

    # ── build Spark session and DataFrames ────────────────────────────────────
    from pyspark.sql import SparkSession

    builder = (
        SparkSession.builder.appName("sentinel-drift")
        .config("spark.sql.shuffle.partitions", "8")
        .config("spark.driver.extraJavaOptions", "-Dlog4j.logLevel=WARN")
    )
    # Only override master for local dev. When submitted via spark-on-k8s-operator
    # the operator sets spark.master before the driver starts — calling .master()
    # here would override that.
    if args.master:
        builder = builder.master(args.master)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")

    ref_df = spark.createDataFrame([(s,) for s in ref_scores], ["score"])
    cur_df = spark.createDataFrame([(s,) for s in cur_scores], ["score"])

    logger.info(
        "DataFrames created | ref_rows=%d | cur_rows=%d",
        ref_df.count(),
        cur_df.count(),
    )

    # ── compute drift (all Spark operations) ──────────────────────────────────
    result = compute_drift(ref_df, cur_df)

    spark.stop()

    # ── write results and report ──────────────────────────────────────────────
    write_drift_stats(
        args.database_url,
        model_version=model_version,
        window_start=window_start,
        window_end=window_end,
        n_samples=len(cur_scores),
        psi=result["psi"],
        jsd=result["jsd"],
        drift_flagged=result["drift_flagged"],
    )

    logger.info("Bin breakdown (ref%% → cur%%):")
    for b in result["bins"]:
        bar = "█" * int(b["current_pct"] / 2)
        logger.info(
            "  %s  ref=%5.1f%%  cur=%5.1f%%  %s",
            b["range"],
            b["reference_pct"],
            b["current_pct"],
            bar,
        )

    status = (
        "DRIFT DETECTED — retrain recommended"
        if result["drift_flagged"]
        else "OK — within threshold"
    )
    logger.info(
        "PSI=%.4f  JSD=%.4f  n=%d  status=%s",
        result["psi"],
        result["jsd"],
        len(cur_scores),
        status,
    )

    sys.exit(2 if result["drift_flagged"] else 0)


if __name__ == "__main__":
    main()
