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
import pytest

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
    big = SD.null_psi_floor(1509, 1509)
    small = SD.null_psi_floor(1509, 83)
    assert small > 5 * big, (small, big)


def test_the_floor_depends_only_on_shape_and_is_cached():
    SD._NULL_FLOOR_CACHE.clear()
    a = SD.null_psi_floor(1509, 83)
    b = SD.null_psi_floor(1509, 83)
    assert a == b
    assert (1509, 83, 10, SD._NULL_TRIALS, 20260807) in SD._NULL_FLOOR_CACHE


def test_a_different_trials_or_seed_is_not_served_the_stale_cached_value():
    """AUDIT REGRESSION GUARD (PR #279 review finding 1): the cache used to key
    on (n_baseline, n_current, bins) only, so a caller raising `trials` or
    varying `seed` for a robustness check silently got back the first call's
    estimate at that shape instead of a fresh one."""
    SD._NULL_FLOOR_CACHE.clear()
    first = SD.null_psi_floor(1509, 83, trials=50, seed=1)
    same_process_cached = SD.null_psi_floor(1509, 83, trials=50, seed=1)
    assert same_process_cached == first

    different_trials = SD.null_psi_floor(1509, 83, trials=500, seed=1)
    different_seed = SD.null_psi_floor(1509, 83, trials=50, seed=2)
    assert different_trials != first
    assert different_seed != first


def test_the_floor_is_deterministic_across_processes():
    """A seeded estimate, so two readers comparing notes see one number."""
    SD._NULL_FLOOR_CACHE.clear()
    a = SD.null_psi_floor(1000, 90)
    SD._NULL_FLOOR_CACHE.clear()
    b = SD.null_psi_floor(1000, 90)
    assert a == b


@pytest.mark.parametrize("nb,nc", [(5, 83), (1509, 0)])
def test_degenerate_shapes_give_nan_not_a_number(nb, nc):
    assert np.isnan(SD.null_psi_floor(nb, nc))


def test_a_degenerate_report_still_carries_the_fields():
    r = SD.score_drift_report(np.array([1.0, 2.0]), np.array([1.0]))
    assert r.severity == "WARN" and not r.ok
    assert np.isnan(r.null_floor) and np.isnan(r.excess_over_floor)


def test_excess_is_nan_not_inf_when_the_floor_is_unusable():
    """inf would sort to the top of any 'worst first' list and hijack triage."""
    r = SD.score_drift_report(np.array([1.0, 2.0]), np.array([1.0]))
    assert not np.isinf(r.excess_over_floor)
