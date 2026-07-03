"""Unit tests for metrics.py — all PySpark, no DB, no network."""

import math

import pytest
from tests.conftest import scores_df


@pytest.fixture
def ref_safe(spark):
    """Reference distribution: all safe scores clustered near 0."""
    import random

    rng = random.Random(42)
    scores = [max(0.0, min(0.09, rng.gauss(0.04, 0.02))) for _ in range(500)]
    return scores_df(spark, scores)


@pytest.fixture
def cur_safe(spark):
    """Current distribution: same safe shape as reference."""
    import random

    rng = random.Random(99)
    scores = [max(0.0, min(0.09, rng.gauss(0.04, 0.02))) for _ in range(500)]
    return scores_df(spark, scores)


@pytest.fixture
def cur_harmful(spark):
    """Current distribution: adversarial traffic, scores near 1."""
    import random

    rng = random.Random(7)
    scores = [max(0.91, min(1.0, rng.gauss(0.95, 0.02))) for _ in range(500)]
    return scores_df(spark, scores)


# ── PSI tests ─────────────────────────────────────────────────────────────────


class TestPSI:
    def test_identical_distributions_give_zero(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe)
        assert result["psi"] < 0.05, (
            f"Expected PSI≈0 for similar distributions, got {result['psi']}"
        )

    def test_severe_drift_exceeds_threshold(self, spark, ref_safe, cur_harmful):
        from metrics import PSI_THRESHOLD, compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        assert result["psi"] > PSI_THRESHOLD, (
            f"Expected PSI>{PSI_THRESHOLD} for severe drift, got {result['psi']}"
        )

    def test_psi_is_non_negative(self, spark, ref_safe, cur_harmful):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        assert result["psi"] >= 0, f"PSI must be non-negative, got {result['psi']}"

    def test_drift_flagged_true_on_severe_drift(self, spark, ref_safe, cur_harmful):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        assert result["drift_flagged"] is True

    def test_drift_flagged_false_on_stable_traffic(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe)
        assert result["drift_flagged"] is False


# ── JSD tests ─────────────────────────────────────────────────────────────────


class TestJSD:
    def test_identical_distributions_give_zero(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe)
        assert result["jsd"] < 0.01, f"Expected JSD≈0, got {result['jsd']}"

    def test_jsd_bounded_by_ln2(self, spark, ref_safe, cur_harmful):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        assert result["jsd"] <= math.log(2) + 1e-9, (
            f"JSD must be ≤ ln(2)={math.log(2):.4f}, got {result['jsd']}"
        )

    def test_jsd_is_non_negative(self, spark, ref_safe, cur_harmful):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        assert result["jsd"] >= 0, f"JSD must be non-negative, got {result['jsd']}"


# ── Bin structure tests ───────────────────────────────────────────────────────


class TestBins:
    def test_always_returns_ten_bins(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe)
        assert len(result["bins"]) == 10

    def test_bin_percentages_sum_to_100(self, spark, ref_safe, cur_harmful):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_harmful)
        ref_total = sum(b["reference_pct"] for b in result["bins"])
        cur_total = sum(b["current_pct"] for b in result["bins"])
        # Epsilon-smoothing means the sum is near 100 but not exactly.
        assert abs(ref_total - 100.0) < 1.0, f"ref bin pcts sum to {ref_total}"
        assert abs(cur_total - 100.0) < 1.0, f"cur bin pcts sum to {cur_total}"

    def test_score_exactly_1_clamps_to_last_bin(self, spark):
        """Scores of 1.0 must land in bin 9 (0.9–1.0), not an out-of-range bin 10."""
        from metrics import compute_drift

        ref = scores_df(spark, [0.5] * 100)
        cur = scores_df(spark, [1.0] * 100)
        result = compute_drift(ref, cur)
        assert len(result["bins"]) == 10
        # All current weight should be in the last bin.
        assert result["bins"][-1]["current_pct"] > 90.0

    def test_bin_ranges_cover_full_unit_interval(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe)
        ranges = [b["range"] for b in result["bins"]]
        assert ranges[0].startswith("0.0"), f"First bin should start at 0.0, got {ranges[0]}"
        assert ranges[-1].endswith("1.0"), f"Last bin should end at 1.0, got {ranges[-1]}"

    def test_empty_bins_still_present(self, spark):
        """If current scores skip middle bins entirely, all 10 bins still appear."""
        from metrics import compute_drift

        ref = scores_df(spark, [0.05] * 200)  # only bin 0
        cur = scores_df(spark, [0.95] * 200)  # only bin 9
        result = compute_drift(ref, cur)
        assert len(result["bins"]) == 10

    def test_n_bins_param_respected(self, spark, ref_safe, cur_safe):
        from metrics import compute_drift

        result = compute_drift(ref_safe, cur_safe, n_bins=5)
        assert len(result["bins"]) == 5
        assert result["n_bins"] == 5
