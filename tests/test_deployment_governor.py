"""Unit tests for the Deployment Governor L1 (RFC 2026-07-09 §2.1, D2).

Contract pinned here:
  * E* = min(Σ_{top-k} min(raw_i, cap_i), E_ceil(regime)) — ceiling only,
    NO exposure floor exists.
  * Hysteresis: |E* − E_current| ≤ band ⇒ E* = E_current (no reallocation).
  * Step limit: E* moves ≤ max_step_per_session × confidence multiplier
    per session.
  * Fail-closed: model_fault=True (or a contract fault) → None; the
    caller falls back to the legacy path.
  * Weak slate is NOT a fault: the Governor returns the LOW E* the slate
    supports plus slate_stats for ledger stamping.
"""
from __future__ import annotations

import math

import pytest

from renquant_pipeline.kernel.deployment_governor import (
    compute_session_target_exposure,
    shrunk_kelly_raw,
)

E_CEIL = {"BULL_CALM": 0.95, "BULL_VOLATILE": 0.7, "CHOPPY": 0.6, "BEAR": 0.35}


def _decide(**overrides):
    kwargs = dict(
        raws={},
        caps={},
        regime="BULL_CALM",
        e_ceil_by_regime=E_CEIL,
        current_gross_exposure=0.0,
        hysteresis_band=0.05,
        confidence=1.0,
        top_k=8,
        max_step_per_session=1.0,   # wide default so tests isolate one rule
        model_fault=False,
    )
    kwargs.update(overrides)
    return compute_session_target_exposure(**kwargs)


# ── shrunk-Kelly raw (formula + guards, same conventions as kernel.kelly) ──

def test_shrunk_kelly_raw_formula():
    # λ·max(μ − s·σ, 0)/σ² with λ=0.3, s=0.5, μ=0.05, σ=0.2 → 0.3·(0.05−0.1)→0
    assert shrunk_kelly_raw(0.05, 0.2, kelly_fraction=0.3, mu_shrinkage=0.5) == 0.0
    # s=0: 0.3 · 0.04 / 0.04 = 0.3
    assert shrunk_kelly_raw(0.04, 0.2, kelly_fraction=0.3, mu_shrinkage=0.0) == pytest.approx(0.3)
    # s=0.1: 0.3 · (0.04 − 0.02) / 0.04 = 0.15
    assert shrunk_kelly_raw(0.04, 0.2, kelly_fraction=0.3, mu_shrinkage=0.1) == pytest.approx(0.15)


@pytest.mark.parametrize("mu,sigma", [
    (None, 0.2), (0.04, None), (float("nan"), 0.2), (0.04, float("nan")),
    (0.04, 0.0), (0.04, -0.1), (-0.04, 0.2), ("bad", 0.2),
])
def test_shrunk_kelly_raw_guards_return_zero(mu, sigma):
    assert shrunk_kelly_raw(mu, sigma, kelly_fraction=0.3, mu_shrinkage=0.0) == 0.0


# ── Ceiling binds ──────────────────────────────────────────────────────────

def test_ceiling_binds_when_slate_exceeds_regime_ceil():
    raws = {f"T{i}": 0.2 for i in range(8)}       # Σ capped = 8 × 0.12 = 0.96
    caps = {t: 0.12 for t in raws}
    d = _decide(raws=raws, caps=caps, regime="BULL_CALM",
                current_gross_exposure=0.9, hysteresis_band=0.0)
    assert d is not None
    assert d.e_raw == pytest.approx(0.96)
    assert d.e_target == pytest.approx(0.95)       # E_ceil(BULL_CALM)
    assert d.ceiling_bound is True


def test_regime_ceiling_map_is_respected_per_regime():
    raws = {f"T{i}": 0.2 for i in range(8)}
    caps = {t: 0.12 for t in raws}
    for regime, ceil in E_CEIL.items():
        d = _decide(raws=raws, caps=caps, regime=regime,
                    current_gross_exposure=ceil, hysteresis_band=0.0)
        assert d.e_target == pytest.approx(ceil), regime


def test_e_raw_sums_capped_raws_over_top_k_only():
    raws = {"A": 0.30, "B": 0.15, "C": 0.06, "D": 0.05}
    caps = {t: 0.12 for t in raws}
    d = _decide(raws=raws, caps=caps, top_k=2, hysteresis_band=0.0)
    # top-2 by raw: A (capped 0.12) + B (capped 0.12) — C, D excluded
    assert d.e_raw == pytest.approx(0.24)
    assert d.e_target == pytest.approx(0.24)


def test_no_exposure_floor_weak_slate_returns_low_e_star():
    """A weak slate yields the LOW E* it supports — never a forced floor."""
    raws = {"A": 0.02}
    d = _decide(raws=raws, caps={"A": 0.12}, hysteresis_band=0.0)
    assert d is not None                            # NOT a fault
    assert d.e_target == pytest.approx(0.02)
    assert d.slate_stats["admitted_count"] == 1
    assert d.slate_stats["weak_slate"] is False


# ── Hysteresis ─────────────────────────────────────────────────────────────

def test_hysteresis_holds_within_band():
    raws = {"A": 0.12}
    d = _decide(raws=raws, caps={"A": 0.12},
                current_gross_exposure=0.10, hysteresis_band=0.05)
    assert d.hysteresis_held is True
    assert d.e_target == pytest.approx(0.10)        # E* collapses to E_current


def test_hysteresis_released_outside_band():
    raws = {"A": 0.30}
    d = _decide(raws=raws, caps={"A": 0.30},
                current_gross_exposure=0.10, hysteresis_band=0.05)
    assert d.hysteresis_held is False
    assert d.e_target == pytest.approx(0.30)


def test_hysteresis_boundary_is_inclusive():
    d = _decide(raws={"A": 0.15}, caps={"A": 0.15},
                current_gross_exposure=0.10, hysteresis_band=0.05)
    assert d.hysteresis_held is True
    assert d.e_target == pytest.approx(0.10)


# ── Step limit ─────────────────────────────────────────────────────────────

def test_step_limit_clamps_upward_move():
    d = _decide(raws={"A": 0.9}, caps={"A": 0.9},
                current_gross_exposure=0.10, hysteresis_band=0.0,
                max_step_per_session=0.15, confidence=1.0)
    assert d.step_limited is True
    assert d.e_target == pytest.approx(0.25)        # 0.10 + 0.15 × 1.0


def test_step_limit_clamps_downward_move():
    d = _decide(raws={"A": 0.05}, caps={"A": 0.05},
                current_gross_exposure=0.60, hysteresis_band=0.0,
                max_step_per_session=0.15, confidence=1.0)
    assert d.step_limited is True
    assert d.e_target == pytest.approx(0.45)        # 0.60 − 0.15 × 1.0


def test_step_limit_scales_with_confidence_multiplier():
    # confidence_to_size_multiplier floors at 0.5: conf 0 → step 0.075.
    d = _decide(raws={"A": 0.9}, caps={"A": 0.9},
                current_gross_exposure=0.10, hysteresis_band=0.0,
                max_step_per_session=0.15, confidence=0.0)
    assert d.e_target == pytest.approx(0.10 + 0.15 * 0.5)
    d_full = _decide(raws={"A": 0.9}, caps={"A": 0.9},
                     current_gross_exposure=0.10, hysteresis_band=0.0,
                     max_step_per_session=0.15, confidence=1.0)
    assert d_full.e_target == pytest.approx(0.25)


def test_step_not_limited_when_move_within_step():
    d = _decide(raws={"A": 0.2}, caps={"A": 0.2},
                current_gross_exposure=0.10, hysteresis_band=0.0,
                max_step_per_session=0.15, confidence=1.0)
    assert d.step_limited is False
    assert d.e_target == pytest.approx(0.2)


# ── Fail-closed ────────────────────────────────────────────────────────────

def test_model_fault_returns_none():
    assert _decide(raws={"A": 0.2}, caps={"A": 0.12}, model_fault=True) is None


def test_unmapped_regime_returns_none():
    assert _decide(raws={"A": 0.2}, caps={"A": 0.12}, regime="NO_SUCH") is None


def test_non_finite_current_exposure_returns_none():
    assert _decide(raws={"A": 0.2}, caps={"A": 0.12},
                   current_gross_exposure=float("nan")) is None


def test_non_numeric_ceiling_returns_none():
    assert _decide(raws={"A": 0.2}, caps={"A": 0.12},
                   e_ceil_by_regime={"BULL_CALM": "oops"}) is None


# ── Weak slate: stats dict for ledger stamping ─────────────────────────────

def test_weak_slate_empty_is_not_a_fault_and_stamps_stats():
    d = _decide(raws={}, caps={}, current_gross_exposure=0.0,
                hysteresis_band=0.0)
    assert d is not None
    assert d.e_raw == 0.0
    assert d.e_target == 0.0
    assert d.slate_stats == {
        "admitted_count": 0,
        "selected_count": 0,
        "sum_raw": 0.0,
        "mu_dispersion": None,
        "weak_slate": True,
    }


def test_slate_stats_counts_sum_and_dispersion():
    raws = {"A": 0.10, "B": 0.05, "C": 0.0, "D": float("nan"), "E": None}
    mu = {"A": 0.04, "B": 0.02, "C": 0.01}
    d = _decide(raws=raws, caps={t: 0.12 for t in raws}, mu=mu,
                hysteresis_band=0.0)
    stats = d.slate_stats
    assert stats["admitted_count"] == 2                 # A, B only
    assert stats["sum_raw"] == pytest.approx(0.15)
    # population stdev of [0.04, 0.02] = 0.01
    assert stats["mu_dispersion"] == pytest.approx(0.01)
    assert stats["weak_slate"] is False


def test_non_finite_raws_are_not_admitted():
    raws = {"A": float("inf"), "B": float("nan"), "C": -0.1, "D": 0.08}
    d = _decide(raws=raws, caps={t: 0.12 for t in raws}, hysteresis_band=0.0)
    assert d.slate_stats["admitted_count"] == 1
    assert d.e_raw == pytest.approx(0.08)


def test_missing_cap_means_uncapped():
    d = _decide(raws={"A": 0.2}, caps={}, hysteresis_band=0.0)
    assert d.e_raw == pytest.approx(0.2)
    assert math.isfinite(d.e_target)


# ── L1 candidate selection (RFC §2.1, r4/r9 review — all three arms) ──────

def test_default_candidate_is_kelly_backward_compatible():
    d = _decide(raws={"A": 0.2}, caps={"A": 0.12}, hysteresis_band=0.0)
    assert d.l1_candidate == "kelly"
    assert d.e_target == pytest.approx(0.12)  # min(e_raw, e_ceil)


def test_candidate_ceil_ignores_e_raw_entirely():
    # Weak slate (e_raw far below ceiling) — candidate (A) still targets
    # the full regime ceiling, independent of conviction.
    raws = {"A": 0.01}
    d = _decide(raws=raws, caps={"A": 0.12}, regime="BULL_CALM",
                hysteresis_band=0.0, l1_candidate="ceil")
    assert d.l1_candidate == "ceil"
    assert d.e_raw == pytest.approx(0.01)          # still reported
    assert d.e_target == pytest.approx(0.95)       # = E_ceil(BULL_CALM)
    assert d.ceiling_bound is True


def test_candidate_ceil_same_every_regime_regardless_of_slate():
    for regime, ceil in E_CEIL.items():
        d = _decide(raws={}, caps={}, regime=regime,
                    current_gross_exposure=ceil, hysteresis_band=0.0,
                    l1_candidate="ceil")
        assert d.e_target == pytest.approx(ceil), regime


def test_candidate_voltarget_uses_vol_target_scale():
    # 60 identical-return days ⇒ realized_vol computable; target_vol=0.15
    # default, floor/ceiling defaults [0.30, 1.50] from compute_vol_target_scale.
    spy_returns = [0.001] * 60
    d = _decide(raws={"A": 0.5}, caps={"A": 0.5}, regime="BULL_CALM",
                hysteresis_band=0.0, l1_candidate="voltarget",
                spy_returns=spy_returns)
    assert d.l1_candidate == "voltarget"
    assert d.e_vol is not None
    assert d.e_target == pytest.approx(min(d.e_vol, 0.95))


def test_candidate_voltarget_too_few_returns_fails_open_to_ceiling():
    # compute_vol_target_scale fails open to 1.0 with <window_days returns;
    # min(1.0, e_ceil) = e_ceil here since e_ceil < 1.0.
    d = _decide(raws={"A": 0.5}, caps={"A": 0.5}, regime="BULL_CALM",
                hysteresis_band=0.0, l1_candidate="voltarget",
                spy_returns=[0.001] * 5)
    assert d.e_vol == pytest.approx(1.0)
    assert d.e_target == pytest.approx(0.95)


def test_unknown_l1_candidate_is_a_contract_fault():
    assert _decide(raws={"A": 0.2}, caps={"A": 0.12},
                   l1_candidate="bogus") is None


def test_kelly_candidate_ceiling_bound_semantics_unchanged():
    # Exact byte-identical boundary semantics check vs the pre-existing
    # strict-inequality ceiling_bound for the default "kelly" candidate.
    raws = {f"T{i}": 0.2 for i in range(8)}
    caps = {t: 0.12 for t in raws}
    d = _decide(raws=raws, caps=caps, regime="BULL_CALM",
                current_gross_exposure=0.9, hysteresis_band=0.0)
    assert d.e_raw == pytest.approx(0.96)
    assert d.ceiling_bound is True
