"""
Drift metrics: PSI and JSD computed entirely in PySpark.

compute_drift() accepts two Spark DataFrames with a 'score' column (float,
0.0–1.0). All binning and metric math runs as Spark operations; only the
final 10-bin aggregated result (10 rows) is collected to the driver.

This scales to arbitrarily large score tables — the aggregation stays
distributed until the very last step.

PSI interpretation:
  < 0.10   — no significant change
  0.10–0.20 — moderate shift, monitor closely
  > 0.20   — significant drift; triggers retrain in Airflow DAG (Phase 7)

JSD is bounded [0, ln(2)] with natural log. It is symmetric and always
finite, making it more robust than KL divergence when a bin is empty in
one distribution.
"""

from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

N_BINS = 10
PSI_THRESHOLD = 0.2
_EPSILON = 1e-6  # smooths empty bins so log is always finite


def _bin_scores(df: DataFrame, n_bins: int) -> DataFrame:
    """Add a 'bin' column: integer 0 to n_bins-1 based on score.

    floor(score * n_bins) gives 0–n_bins, so scores of exactly 1.0 land in
    bin n_bins — clamped to n_bins-1 with least().
    """
    return df.withColumn(
        "bin",
        F.least(
            F.floor(F.col("score") * n_bins).cast("int"),
            F.lit(n_bins - 1),
        ),
    )


def compute_drift(
    ref_df: DataFrame,
    cur_df: DataFrame,
    n_bins: int = N_BINS,
) -> dict:
    """Compute PSI and JSD between two score distributions using Spark.

    Args:
        ref_df: Spark DataFrame with column 'score' — the reference
                (training-time) distribution.
        cur_df: Spark DataFrame with column 'score' — the current
                (recent inference) distribution.
        n_bins: Number of equal-width bins over [0, 1]. Default 10.

    Returns dict with keys: psi, jsd, drift_flagged, n_bins, bins.
    """
    spark = ref_df.sparkSession

    # ── Step 1: bin both DataFrames ──────────────────────────────────────────
    ref_binned = _bin_scores(ref_df, n_bins)
    cur_binned = _bin_scores(cur_df, n_bins)

    # ── Step 2: count per bin ─────────────────────────────────────────────────
    ref_counts = ref_binned.groupBy("bin").agg(F.count("*").alias("ref_count"))
    cur_counts = cur_binned.groupBy("bin").agg(F.count("*").alias("cur_count"))

    # ── Step 3: ensure all bins are represented (fill missing bins with 0) ───
    # Without this, bins with no scores are absent from the join and PSI/JSD
    # would be computed on fewer than n_bins terms.
    all_bins = spark.range(n_bins).withColumnRenamed("id", "bin")
    ref_counts = all_bins.join(ref_counts, "bin", "left").fillna({"ref_count": 0})
    cur_counts = all_bins.join(cur_counts, "bin", "left").fillna({"cur_count": 0})

    # ── Step 4: total counts for proportion calculation ───────────────────────
    ref_total = ref_df.count()
    cur_total = cur_df.count()

    # ── Step 5: join bins and compute smoothed proportions ───────────────────
    combined = ref_counts.join(cur_counts, "bin")

    # q = reference proportion, p = current proportion (epsilon-smoothed)
    combined = combined.withColumn(
        "q", (F.col("ref_count") + _EPSILON) / (ref_total + n_bins * _EPSILON),
    ).withColumn(
        "p", (F.col("cur_count") + _EPSILON) / (cur_total + n_bins * _EPSILON),
    )

    # ── Step 6: PSI contribution per bin: (p − q) × ln(p / q) ───────────────
    combined = combined.withColumn(
        "psi_contrib",
        (F.col("p") - F.col("q")) * F.log(F.col("p") / F.col("q")),
    )

    # ── Step 7: JSD contribution per bin ─────────────────────────────────────
    # M = midpoint distribution; JSD = 0.5×KL(P‖M) + 0.5×KL(Q‖M)
    combined = combined.withColumn(
        "m", (F.col("p") + F.col("q")) / 2.0,
    ).withColumn(
        "jsd_contrib",
        0.5 * F.col("p") * F.log(F.col("p") / F.col("m"))
        + 0.5 * F.col("q") * F.log(F.col("q") / F.col("m")),
    )

    # ── Step 8: collect — only 10 rows hit the driver ────────────────────────
    combined.explain()
    rows = combined.orderBy("bin").collect()

    psi_val = float(sum(r["psi_contrib"] for r in rows))
    jsd_val = float(sum(r["jsd_contrib"] for r in rows))

    bins = [
        {
            "range": f"{r['bin'] / n_bins:.1f}–{(r['bin'] + 1) / n_bins:.1f}",
            "reference_pct": round(float(r["q"]) * 100, 2),
            "current_pct": round(float(r["p"]) * 100, 2),
        }
        for r in rows
    ]

    return {
        "psi": round(psi_val, 6),
        "jsd": round(jsd_val, 6),
        "drift_flagged": psi_val > PSI_THRESHOLD,
        "n_bins": n_bins,
        "bins": bins,
    }
