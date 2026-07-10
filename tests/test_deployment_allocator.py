"""Unit tests for the down-only deployment allocator (RFC §2.2, D3).

Invariants pinned here:
  * No output weight EVER exceeds its per-name cap (module assert +
    black-box checks across constraint mixes).
  * Every projection (sector / corr-pair / no-buy) is DOWN-ONLY: no
    weight is ever raised, lowest conviction is trimmed first.
  * Σw > E* → proportional scale-down by exactly E*/Σw.
  * E_final ≤ E* (no floors), residual accounting exact:
    residual == E* − E_final.
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.deployment_allocator import allocate_down_only


def _alloc(**overrides):
    kwargs = dict(
        raws={},
        caps={},
        e_star=1.0,
        top_k=8,
    )
    kwargs.update(overrides)
    return allocate_down_only(**kwargs)


# ── Step (a): min(raw, cap), top-k by conviction ───────────────────────────

def test_weights_are_capped_raws():
    res = _alloc(raws={"A": 0.30, "B": 0.05}, caps={"A": 0.12, "B": 0.12})
    assert res.weights == {"A": pytest.approx(0.12), "B": pytest.approx(0.05)}
    assert "A" in res.binding_constraints["per_name_cap"]
    assert "B" not in res.binding_constraints["per_name_cap"]


def test_top_k_selects_by_conviction():
    raws = {"A": 0.10, "B": 0.08, "C": 0.06, "D": 0.04}
    res = _alloc(raws=raws, caps={t: 0.12 for t in raws}, top_k=2)
    assert set(res.weights) == {"A", "B"}
    assert set(res.binding_constraints["top_k_dropped"]) == {"C", "D"}


def test_non_positive_or_non_finite_raws_not_allocated():
    raws = {"A": 0.0, "B": -0.1, "C": float("nan"), "D": None, "E": 0.05}
    res = _alloc(raws=raws, caps={t: 0.12 for t in raws})
    assert set(res.weights) == {"E"}


# ── Invariant: never above cap ─────────────────────────────────────────────

def test_invariant_no_weight_above_cap_across_mixes():
    raws = {f"T{i}": 0.05 * (i + 1) for i in range(10)}
    caps = {t: 0.12 for t in raws}
    sectors = {t: ("tech" if i % 2 == 0 else "energy")
               for i, t in enumerate(raws)}
    res = _alloc(
        raws=raws, caps=caps, e_star=0.5, top_k=8,
        sector_by_name=sectors,
        sector_caps={"tech": 0.2, "energy": 0.25},
        corr_pair_caps=[("T9", "T8", 0.15)],
    )
    for name, w in res.weights.items():
        assert w <= caps[name] + 1e-9, name
    assert res.e_final <= 0.5 + 1e-9


# ── Step (b): down-only projections, lowest conviction trimmed first ──────

def test_sector_trim_is_down_only_lowest_conviction_first():
    raws = {"HI": 0.10, "MID": 0.08, "LO": 0.06}     # all tech
    caps = {t: 0.12 for t in raws}
    res = _alloc(
        raws=raws, caps=caps, e_star=1.0,
        sector_by_name={t: "tech" for t in raws},
        sector_caps={"tech": 0.18},                   # load 0.24 → trim 0.06
    )
    # LO (lowest conviction) absorbs the whole trim; HI/MID untouched.
    assert res.weights["HI"] == pytest.approx(0.10)
    assert res.weights["MID"] == pytest.approx(0.08)
    assert res.weights.get("LO", 0.0) == pytest.approx(0.0, abs=1e-9)
    assert res.binding_constraints["sector_cap"] == {"tech": True}
    # Down-only: no weight rose above its pre-projection value.
    assert all(res.weights.get(t, 0.0) <= raws[t] + 1e-9 for t in raws)


def test_sector_trim_cascades_to_next_lowest_when_needed():
    raws = {"HI": 0.10, "MID": 0.08, "LO": 0.06}
    res = _alloc(
        raws=raws, caps={t: 0.12 for t in raws},
        sector_by_name={t: "tech" for t in raws},
        sector_caps={"tech": 0.15},                   # trim 0.09 > LO's 0.06
    )
    assert res.weights["HI"] == pytest.approx(0.10)
    assert res.weights["MID"] == pytest.approx(0.05)  # 0.08 − 0.03
    assert "LO" not in res.weights                    # fully trimmed
    assert res.e_final == pytest.approx(0.15)


def test_corr_pair_trim_is_down_only_lowest_conviction_first():
    raws = {"HI": 0.10, "LO": 0.08, "OTHER": 0.05}
    res = _alloc(
        raws=raws, caps={t: 0.12 for t in raws},
        corr_pair_caps=[("HI", "LO", 0.12)],          # pair 0.18 → trim 0.06
    )
    assert res.weights["HI"] == pytest.approx(0.10)   # higher conviction kept
    assert res.weights["LO"] == pytest.approx(0.02)   # 0.08 − 0.06
    assert res.weights["OTHER"] == pytest.approx(0.05)
    assert ("HI", "LO") in res.binding_constraints["corr_pair_cap"]


def test_no_buy_mask_clips_to_current_weight():
    raws = {"HELD": 0.10, "NEW": 0.08}
    res = _alloc(
        raws=raws, caps={t: 0.12 for t in raws},
        current_weights={"HELD": 0.04},
        no_buy={"HELD"},
    )
    assert res.weights["HELD"] == pytest.approx(0.04)  # cannot increase
    assert res.weights["NEW"] == pytest.approx(0.08)
    assert "HELD" in res.binding_constraints["no_buy"]


def test_no_buy_mask_drops_unheld_name():
    res = _alloc(raws={"A": 0.10}, caps={"A": 0.12}, no_buy={"A"})
    assert res.weights == {}


# ── Step (c): E* scale-down exact ──────────────────────────────────────────

def test_e_star_scaling_is_exact_proportional_down():
    raws = {"A": 0.12, "B": 0.12, "C": 0.06}          # Σ = 0.30
    res = _alloc(raws=raws, caps={t: 0.12 for t in raws}, e_star=0.15)
    factor = 0.15 / 0.30
    assert res.weights["A"] == pytest.approx(0.12 * factor)
    assert res.weights["B"] == pytest.approx(0.12 * factor)
    assert res.weights["C"] == pytest.approx(0.06 * factor)
    assert res.e_final == pytest.approx(0.15)
    assert res.residual == pytest.approx(0.0)
    assert res.binding_constraints["e_star_scaled"] is True


def test_no_scaling_when_sum_below_e_star():
    raws = {"A": 0.05, "B": 0.04}
    res = _alloc(raws=raws, caps={t: 0.12 for t in raws}, e_star=0.5)
    assert res.weights["A"] == pytest.approx(0.05)
    assert res.weights["B"] == pytest.approx(0.04)
    assert res.binding_constraints["e_star_scaled"] is False


# ── Steps (d)/(e): E_final ≤ E*, exact residual accounting ────────────────

def test_e_final_never_exceeds_e_star():
    for e_star in (0.0, 0.05, 0.15, 0.3, 1.0):
        res = _alloc(raws={"A": 0.12, "B": 0.10}, caps={"A": 0.12, "B": 0.12},
                     e_star=e_star)
        assert res.e_final <= e_star + 1e-9, e_star


def test_residual_accounting_exact_weak_slate():
    res = _alloc(raws={}, caps={}, e_star=0.4)
    assert res.weights == {}
    assert res.e_final == 0.0
    assert res.residual == pytest.approx(0.4)


def test_residual_accounting_exact_with_binding_sector_cap():
    # E* = 0.5, capped raws Σ = 0.24, sector cap trims tech to 0.18 →
    # E_final 0.18, residual EXACTLY 0.32 with the binder recorded.
    raws = {"A": 0.12, "B": 0.12}
    res = _alloc(
        raws=raws, caps={t: 0.12 for t in raws}, e_star=0.5,
        sector_by_name={"A": "tech", "B": "tech"},
        sector_caps={"tech": 0.18},
    )
    assert res.e_final == pytest.approx(0.18)
    assert res.residual == pytest.approx(0.32)
    assert res.binding_constraints["sector_cap"] == {"tech": True}


def test_residual_is_e_star_minus_e_final_everywhere():
    res = _alloc(raws={"A": 0.07}, caps={"A": 0.12}, e_star=0.25)
    assert res.residual == pytest.approx(0.25 - res.e_final)
    assert res.residual == pytest.approx(0.18)


# ── No-sell floors (RFC §1.3 masks entering L2) ────────────────────────────

def test_no_sell_floor_keeps_held_weight_out_of_top_k():
    # Held name with zero raw (model soured) but under min-hold: its
    # current weight is a floor; new capital still goes to the slate.
    res = _alloc(
        raws={"NEW": 0.10, "HELD": 0.0}, caps={"NEW": 0.12, "HELD": 0.12},
        e_star=0.5, current_weights={"HELD": 0.06}, no_sell={"HELD"},
    )
    assert res.weights["HELD"] == pytest.approx(0.06)
    assert res.weights["NEW"] == pytest.approx(0.10)
    assert "HELD" in res.binding_constraints["no_sell_floor"]


def test_no_sell_floor_exempt_from_e_star_scaling():
    # Σ = 0.06 (floor) + 0.24 (reducible) = 0.30, E* = 0.18 →
    # only the reducible mass scales: factor = 1 − 0.12/0.24 = 0.5.
    res = _alloc(
        raws={"A": 0.12, "B": 0.12}, caps={"A": 0.12, "B": 0.12, "HELD": 0.12},
        e_star=0.18, current_weights={"HELD": 0.06}, no_sell={"HELD"},
    )
    assert res.weights["HELD"] == pytest.approx(0.06)   # floor untouched
    assert res.weights["A"] == pytest.approx(0.06)
    assert res.weights["B"] == pytest.approx(0.06)
    assert res.e_final == pytest.approx(0.18)
    assert res.residual == pytest.approx(0.0)


def test_no_sell_floor_never_added_above_cap_by_allocation():
    # Drifted position above cap: the floor wins (mask cannot force a
    # sell) but the allocator never ADDS to it past the cap.
    res = _alloc(
        raws={"HELD": 0.30}, caps={"HELD": 0.12},
        e_star=0.5, current_weights={"HELD": 0.14}, no_sell={"HELD"},
    )
    assert res.weights["HELD"] == pytest.approx(0.14)   # floor, not raw/cap
