"""Anytime-valid confidence sequence (brief section 7, Phase 11)."""

from __future__ import annotations

import numpy as np

from lab.eval.anytime import confidence_sequence, cs_sequence_over_time


def test_confidence_sequence_centers_on_sample_mean():
    diffs = np.array([0.1, 0.2, -0.05, 0.15, 0.0])
    cs = confidence_sequence(diffs, alpha=0.05)
    assert cs.center == np.mean(diffs)
    assert cs.n == 5
    assert cs.lo < cs.center < cs.hi
    assert cs.radius > 0


def test_cs_sequence_over_time_one_entry_per_prefix():
    diffs = np.array([0.1, 0.2, -0.1, 0.05])
    seq = cs_sequence_over_time(diffs, alpha=0.05)
    assert len(seq) == 4
    assert [cs.n for cs in seq] == [1, 2, 3, 4]
    # Zero variance at t=1 (a single point) -> the interval is still finite
    # and centered exactly on that first observation.
    assert seq[0].center == diffs[0]


def test_confidence_sequence_empty_input_covers_everything():
    cs = confidence_sequence(np.array([]))
    assert cs.n == 0
    assert cs.covers_zero is True
    assert cs.lo == -float("inf") and cs.hi == float("inf")


def test_cs_null_coverage_at_least_95_percent_across_all_look_times():
    """Phase 11 acceptance criterion: on simulated null data (true mean
    zero), the confidence sequence must cover zero at EVERY look time in at
    least 95% of repetitions -- the defining time-uniform coverage guarantee
    (alpha=0.05), not just at a single fixed sample size."""
    rng = np.random.default_rng(42)
    n_reps = 500
    seq_len = 150
    alpha = 0.05
    all_covered_count = 0
    for _ in range(n_reps):
        # Bounded, mean-zero null process resembling paired Brier differences.
        diffs = rng.uniform(-0.3, 0.3, seq_len)
        seq = cs_sequence_over_time(diffs, alpha=alpha)
        if all(cs.covers_zero for cs in seq):
            all_covered_count += 1
    coverage_rate = all_covered_count / n_reps
    assert coverage_rate >= 0.95, f"coverage_rate={coverage_rate} (expected >= 0.95)"


def test_cs_eventually_excludes_zero_under_a_real_effect():
    """Sanity check (not itself an acceptance criterion): with a planted
    nonzero mean, the CS should eventually and durably exclude zero -- the
    construction isn't so conservative it never detects a real effect."""
    rng = np.random.default_rng(7)
    diffs = rng.uniform(-0.3, 0.3, 400) + 0.15  # clear positive skill
    seq = cs_sequence_over_time(diffs, alpha=0.05)
    assert not seq[-1].covers_zero
    assert seq[-1].lo > 0
