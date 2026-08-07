"""The noise floor is reported so a PSI can be read; it never gates.

Context measured 2026-08-07 over 1,082 live audits. `n_current` was under 100
EVERY time (median 83) against an ~1,500 baseline, and at that shape the
zero-drift median PSI is ~0.118 — about 7x the ~0.016 of a matched comparison.
The textbook CRITICAL band (0.25) therefore sits barely 2x above where a
perfectly stable model lands.

What re-banding the live audits by `excess = psi / floor` then showed is the
opposite of a false-alarm story, and it is why these tests pin the field rather
than any threshold change: **zero** CRITICAL rows sit below the floor, and the
median audit is 2.49x above it. The alarm has never fired on pure small-sample
noise; the drift is real. The floor makes that legible instead of arguable.
"""
from __future__ import annotations

import numpy as np

from renquant_pipeline.kernel import score_drift as SD


def test_the_floor_is_reported_and_changes_no_verdict():
    rng = np.random.default_rng(7)
    base, cur = rng.normal(size=1509), rng.normal(size=83)
    r = SD.score_drift_report(base, cur)
    assert r.severity == SD.severity(r.psi), "the band must still come from psi alone"
    assert np.isfinite(r.null_floor) and r.null_floor > 0


def test_zero_drift_lands_at_or_below_the_floor():
    rng = np.random.default_rng(11)
    r = SD.score_drift_report(rng.normal(size=1509), rng.normal(size=83))
    assert r.excess_over_floor < 1.6, (
        "same-law samples must sit near the floor; a large excess here means "
        "the floor estimate is wrong", r)


def test_a_real_shift_stands_well_above_the_floor():
    rng = np.random.default_rng(11)
    r = SD.score_drift_report(rng.normal(size=1509), rng.normal(loc=0.5, size=83))
    assert r.excess_over_floor > 2.0, r


def test_the_floor_rises_as_the_current_sample_shrinks():
    """The whole point: the SAME model looks worse at a smaller n."""
    base = np.random.default_rng(3).normal(size=1509)
    big = SD.null_psi_floor(base, 1509)
    small = SD.null_psi_floor(base, 83)
    assert small > 5 * big, (small, big)


def test_the_floor_is_deterministic_for_the_same_baseline_and_is_cached():
    SD._NULL_FLOOR_CACHE.clear()
    base = np.random.default_rng(5).normal(size=1509)
    a = SD.null_psi_floor(base, 83)
    b = SD.null_psi_floor(base, 83)
    assert a == b
    assert len(SD._NULL_FLOOR_CACHE) == 1


def test_a_different_trials_or_seed_is_not_served_the_stale_cached_value():
    """AUDIT REGRESSION GUARD (PR #279 review finding 1): the cache used to key
    on (n_baseline, n_current, bins) only, so a caller raising `trials` or
    varying `seed` for a robustness check silently got back the first call's
    estimate at that shape instead of a fresh one."""
    SD._NULL_FLOOR_CACHE.clear()
    base = np.random.default_rng(9).normal(size=1509)
    first = SD.null_psi_floor(base, 83, trials=50, seed=1)
    same_process_cached = SD.null_psi_floor(base, 83, trials=50, seed=1)
    assert same_process_cached == first

    different_trials = SD.null_psi_floor(base, 83, trials=500, seed=1)
    different_seed = SD.null_psi_floor(base, 83, trials=50, seed=2)
    assert different_trials != first
    assert different_seed != first


def test_the_floor_is_deterministic_across_processes():
    """A seeded estimate, so two readers comparing notes see one number."""
    SD._NULL_FLOOR_CACHE.clear()
    base = np.random.default_rng(13).normal(size=1000)
    a = SD.null_psi_floor(base, 90)
    SD._NULL_FLOOR_CACHE.clear()
    b = SD.null_psi_floor(base, 90)
    assert a == b


def test_a_tied_baseline_floor_is_conditioned_on_the_real_distribution():
    """AUDIT REGRESSION GUARD (PR #279 review, P1, repeated across 3 rounds):
    `psi()` bins on `np.quantile(expected, ...)`, so a tied/discrete baseline
    collapses those edges into fewer effective bins. A shape-only Gaussian
    simulation of the same SIZE does not see the collapse and overstates the
    floor. Reviewer's own repro on `np.repeat(np.arange(5), 300)`: shape-only
    ~0.1189 vs the real-baseline-conditioned ~0.0370 — resampling from the
    baseline itself must land near the low, correct value, not the inflated
    shape-only one."""
    SD._NULL_FLOOR_CACHE.clear()
    tied = np.repeat(np.arange(5, dtype=float), 300)
    conditioned = SD.null_psi_floor(tied, 83)

    rng = np.random.default_rng(20260807)
    shape_only_gaussian = float(np.median([
        SD.psi(rng.standard_normal(tied.size), rng.standard_normal(83), bins=10)
        for _ in range(SD._NULL_TRIALS)
    ]))
    assert conditioned < shape_only_gaussian / 2, (conditioned, shape_only_gaussian)


def test_a_baseline_that_shares_the_old_21point_grid_gets_its_own_floor():
    """AUDIT REGRESSION GUARD (PR #279 review, 5th round): `_baseline_key()`
    used to identify a baseline by a fixed 21-point (5%) quantile grid,
    independent of `bins`. Two distinct baselines can share that coarse grid
    while `psi()` — which bins on the REQUESTED `bins`, not on a fixed 21
    points — reads different edges/counts from them, especially once
    `bins > 20`. The old key would then silently serve baseline A's cached
    floor to a query for baseline B.

    Construct exactly that pair (61 elements so `bins=30` ranks are also
    exact integers, no interpolation to reason about): fix the 21
    ventile-defining points identically across both, and vary everything
    else so `bins=30` reads different content.
    """
    a, b = [], []
    for k in range(20):
        a += [float(k), float(k), float(k)]
        b += [float(k), k + 0.3, k + 0.6]
    a.append(20.0)
    b.append(20.0)
    a, b = np.array(a), np.array(b)
    assert a.size == b.size == 61

    ventiles = np.linspace(0, 1, 21)
    assert np.allclose(np.quantile(a, ventiles), np.quantile(b, ventiles)), (
        "test premise: a and b must share the old fixed 21-point key")
    thirty_bin_edges = np.linspace(0, 1, 31)
    assert not np.allclose(np.quantile(a, thirty_bin_edges), np.quantile(b, thirty_bin_edges)), (
        "test premise: a and b must differ at the bins=30 resolution psi() actually reads")

    SD._NULL_FLOOR_CACHE.clear()
    floor_a_first = SD.null_psi_floor(a, 40, bins=30)
    floor_b_after_a = SD.null_psi_floor(b, 40, bins=30)
    SD._NULL_FLOOR_CACHE.clear()
    floor_b_fresh = SD.null_psi_floor(b, 40, bins=30)

    assert floor_b_after_a == floor_b_fresh, (
        "b's result right after a must equal a cache-cleared call for b alone "
        "— the old key would have failed this by returning a's floor",
        floor_a_first, floor_b_after_a, floor_b_fresh)
    assert floor_b_after_a != floor_a_first, "a and b are genuinely different baselines"


def test_small_baseline_gives_nan_not_a_number():
    assert np.isnan(SD.null_psi_floor(np.zeros(5), 83))


def test_zero_current_gives_nan_not_a_number():
    assert np.isnan(SD.null_psi_floor(np.random.default_rng(1).normal(size=1509), 0))


def test_a_degenerate_report_still_carries_the_fields():
    r = SD.score_drift_report(np.array([1.0, 2.0]), np.array([1.0]))
    assert r.severity == "WARN" and not r.ok
    assert np.isnan(r.null_floor) and np.isnan(r.excess_over_floor)


def test_excess_is_nan_not_inf_when_the_floor_is_unusable():
    """inf would sort to the top of any 'worst first' list and hijack triage."""
    r = SD.score_drift_report(np.array([1.0, 2.0]), np.array([1.0]))
    assert not np.isinf(r.excess_over_floor)
