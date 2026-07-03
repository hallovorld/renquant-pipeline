"""BL-1 / M4: per-bar raw recentering ahead of the global calibrator.

Live evidence (2026-07-01/02 prod): the pooled calibrator's ER=0 neutral sits
at raw=−0.2902, so raw scores in (−0.2902, 0) map to a μ of the OPPOSITE sign
to their raw signal — ``calibrator_sign_laundered`` counted 44/~90 (07-01) and
45/~90 (07-02) candidates. BL-1 translates each bar's raw cross-section so its
MEDIAN lands on the calibrator's neutral anchor before the interpolation heads
run, under ``ranking.panel_scoring.global_calibration.recenter_raw_per_bar``
(default OFF).

These tests pin:
  1. Flag absent / false ⇒ byte-identical legacy outputs (regression).
  2. Flag on ⇒ the per-bar center is removed: the median name gets μ=0
     exactly (neutral alignment by construction), above-center names μ>0,
     below-center μ<0, rank ORDER preserved, and the BL-2
     ``calibrator_sign_laundered`` counter collapses to 0.
  3. ``c.panel_score`` is never mutated (downstream raw consumers untouched).
  4. Holdings share the SAME per-bar shift as candidates.
  5. NaN/None raw scores are excluded from the center and skipped, as legacy.
  6. Safe fallbacks: no ER=0 anchor or a too-thin cross-section ⇒ raw path.
  7. The Kelly μ head (``use_calibrator_mu``) is recentered consistently.
"""
from __future__ import annotations

import datetime
import math

import numpy as np
import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.exits import HoldingState
from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    ApplyGlobalCalibrationTask,
)
from renquant_pipeline.kernel.selection import CandidateResult


# ── fixtures ─────────────────────────────────────────────────────────────────

def _live_like_calibrator(**meta) -> GlobalPanelCalibration:
    """Monotone heads with the LIVE pathology: ER=0 neutral at raw=−0.29."""
    return GlobalPanelCalibration(
        prob_x=np.array([-0.60, -0.29, 0.00, 0.60]),
        prob_y=np.array([0.35, 0.50, 0.56, 0.72]),
        er_x=np.array([-0.60, -0.29, 0.00, 0.60]),
        er_y=np.array([-0.030, 0.000, 0.028, 0.070]),
        metadata={"lookahead_days_used": 60, **meta},
    )


def _cand(ticker: str, ps: float | None) -> CandidateResult:
    return CandidateResult(
        ticker=ticker, raw_score=0.0, rank_score=0.0, rs_score=0.0,
        panel_score=ps,
    )


def _ctx(
    scores: dict[str, float | None],
    cal: GlobalPanelCalibration,
    *,
    flag: bool | None = None,
    holdings: dict[str, float] | None = None,
    use_calibrator_mu: bool = False,
) -> InferenceContext:
    gc_cfg: dict = {"enabled": True}
    if flag is not None:
        gc_cfg["recenter_raw_per_bar"] = flag
    ctx = InferenceContext(
        config={
            "ranking": {
                "panel_scoring": {
                    "enabled": True,
                    "global_calibration": gc_cfg,
                },
                "kelly_sizing": {"use_calibrator_mu": use_calibrator_mu},
            },
        },
        today=datetime.date(2026, 7, 2),
    )
    ctx.candidates = [_cand(t, ps) for t, ps in scores.items()]
    for t, ps in (holdings or {}).items():
        hs = HoldingState(
            entry_price=100.0, entry_date=datetime.date(2026, 6, 1),
            high_watermark=100.0,
        )
        hs.panel_score = ps
        ctx.holdings[t] = hs
    ctx._global_calibrator = cal  # preloaded (LoadGlobalCalibrationTask skipped)
    return ctx


# Live-shaped cross-section: all-negative raws centred at −0.20, i.e. every
# name is "bearish vs 0" but half are above the bar's own center. Names in
# (−0.29, 0) are the laundered ones under legacy behavior.
LIVE_SHAPED = {
    "AAA": -0.45, "BBB": -0.30, "CCC": -0.20, "DDD": -0.10, "EEE": -0.05,
}


# ── 1. flag absent / false ⇒ byte-identical legacy behavior ─────────────────

def _snapshot(ctx: InferenceContext) -> list[tuple]:
    return [
        (c.ticker, c.panel_score, c.rank_score, c.expected_return,
         c.expected_return_horizon_days, c.mu, c.mu_horizon_days)
        for c in ctx.candidates
    ]


@pytest.mark.parametrize("flag", [None, False])
def test_flag_off_is_byte_identical_to_legacy(flag) -> None:
    cal = _live_like_calibrator()
    ctx = _ctx(LIVE_SHAPED, cal, flag=flag)
    assert ApplyGlobalCalibrationTask().run(ctx) is not False

    for c in ctx.candidates:
        raw = LIVE_SHAPED[c.ticker]
        assert c.panel_score == raw  # untouched
        assert c.rank_score == pytest.approx(cal.calibrate_probability(raw))
        assert c.expected_return == pytest.approx(cal.expected_return(raw))
    # Legacy BL-2 count: raw<0 with ER>0 → the 3 names above neutral −0.29
    # (CCC, DDD, EEE) launder; BBB sits below neutral (ER<0), AAA is deep
    # negative.
    assert ctx.counters["calibrator_sign_laundered"] == 3


def test_flag_false_equals_flag_absent_exactly() -> None:
    cal = _live_like_calibrator()
    ctx_absent = _ctx(LIVE_SHAPED, cal, flag=None)
    ctx_false = _ctx(LIVE_SHAPED, cal, flag=False)
    ApplyGlobalCalibrationTask().run(ctx_absent)
    ApplyGlobalCalibrationTask().run(ctx_false)
    assert _snapshot(ctx_absent) == _snapshot(ctx_false)
    assert ctx_absent.counters == ctx_false.counters


# ── 2. flag on ⇒ center removed / neutral aligned / counter collapses ───────

def test_recenter_aligns_median_to_neutral_and_kills_laundering() -> None:
    cal = _live_like_calibrator()
    ctx = _ctx(LIVE_SHAPED, cal, flag=True)
    assert ApplyGlobalCalibrationTask().run(ctx) is not False

    by_ticker = {c.ticker: c for c in ctx.candidates}
    # Median name (CCC at −0.20 = the bar's center) sits exactly on the
    # calibrator's neutral by construction → μ = 0.
    assert by_ticker["CCC"].expected_return == pytest.approx(0.0, abs=1e-12)
    # Above-center names → μ>0; below-center names → μ<0. The raw signs
    # (all negative) no longer decide the μ sign — the cross-sectional
    # stance does.
    assert by_ticker["DDD"].expected_return > 0.0
    assert by_ticker["EEE"].expected_return > 0.0
    assert by_ticker["AAA"].expected_return < 0.0
    assert by_ticker["BBB"].expected_return < 0.0
    # BL-2 acceptance metric: residual laundering (recentered sign vs μ
    # sign) is zero on a strictly-monotone head.
    assert ctx.counters["calibrator_sign_laundered"] == 0
    # Rank ORDER is preserved (the shift is a translation; heads monotone).
    ordered = sorted(ctx.candidates, key=lambda c: c.panel_score)
    ranks = [c.rank_score for c in ordered]
    assert ranks == sorted(ranks)
    # Raw scores are NOT mutated.
    for c in ctx.candidates:
        assert c.panel_score == LIVE_SHAPED[c.ticker]


def test_recenter_exact_shift_arithmetic() -> None:
    """μ(raw) under the flag == μ_legacy(raw − center + neutral)."""
    cal = _live_like_calibrator()
    ctx = _ctx(LIVE_SHAPED, cal, flag=True)
    ApplyGlobalCalibrationTask().run(ctx)
    center = float(np.median(list(LIVE_SHAPED.values())))
    anchor = cal.neutral_raw
    assert anchor == pytest.approx(-0.29)
    for c in ctx.candidates:
        x = LIVE_SHAPED[c.ticker] - center + anchor
        assert c.expected_return == pytest.approx(cal.expected_return(x))
        assert c.rank_score == pytest.approx(cal.calibrate_probability(x))


# ── 3/4. holdings share the same per-bar shift ───────────────────────────────

def test_holdings_recentered_with_candidate_shift() -> None:
    cal = _live_like_calibrator()
    # HOLD1 sits exactly at the candidate median → μ=0; HOLD2 above → μ>0.
    # Holdings do NOT contribute to the center (candidates own the panel).
    ctx = _ctx(
        LIVE_SHAPED, cal, flag=True,
        holdings={"HOLD1": -0.20, "HOLD2": -0.05},
    )
    ApplyGlobalCalibrationTask().run(ctx)
    assert ctx.holdings["HOLD1"].expected_return == pytest.approx(0.0, abs=1e-12)
    assert ctx.holdings["HOLD2"].expected_return > 0.0
    assert ctx.holdings["HOLD2"].panel_score == -0.05  # untouched
    # Same raw ⇒ same calibrated outputs as the matching candidate.
    cand_eee = next(c for c in ctx.candidates if c.ticker == "EEE")
    assert ctx.holdings["HOLD2"].expected_return == pytest.approx(
        cand_eee.expected_return
    )


# ── 5. NaN robustness ────────────────────────────────────────────────────────

def test_nan_and_none_scores_excluded_from_center_and_skipped() -> None:
    cal = _live_like_calibrator()
    scores = {**LIVE_SHAPED, "NAN": float("nan"), "NONE": None}
    ctx = _ctx(scores, cal, flag=True)
    ApplyGlobalCalibrationTask().run(ctx)
    by_ticker = {c.ticker: c for c in ctx.candidates}
    # NaN/None candidates are skipped exactly as legacy (defaults intact).
    assert by_ticker["NAN"].rank_score == 0.0
    assert by_ticker["NONE"].rank_score == 0.0
    # And they must not poison the center: median over the 5 finite scores
    # is still −0.20 → CCC still lands exactly on neutral.
    assert by_ticker["CCC"].expected_return == pytest.approx(0.0, abs=1e-12)
    assert ctx.counters["calibrator_sign_laundered"] == 0


def test_nan_holding_skipped_under_flag() -> None:
    cal = _live_like_calibrator()
    ctx = _ctx(LIVE_SHAPED, cal, flag=True, holdings={"HNAN": float("nan")})
    ApplyGlobalCalibrationTask().run(ctx)
    assert ctx.holdings["HNAN"].rank_score is None  # untouched, as legacy


# ── 6. safe fallbacks: no anchor / thin cross-section ⇒ raw path ────────────

def test_no_neutral_anchor_falls_back_to_raw_path() -> None:
    # All-positive ER head → neutral_raw is None → nothing to align onto.
    cal = GlobalPanelCalibration(
        prob_x=np.array([-0.60, 0.00, 0.60]),
        prob_y=np.array([0.40, 0.55, 0.70]),
        er_x=np.array([-0.60, 0.00, 0.60]),
        er_y=np.array([0.010, 0.030, 0.070]),
        metadata={"lookahead_days_used": 60},
    )
    assert cal.neutral_raw is None
    ctx_on = _ctx(LIVE_SHAPED, cal, flag=True)
    ctx_off = _ctx(LIVE_SHAPED, cal, flag=False)
    ApplyGlobalCalibrationTask().run(ctx_on)
    ApplyGlobalCalibrationTask().run(ctx_off)
    assert _snapshot(ctx_on) == _snapshot(ctx_off)


def test_thin_cross_section_falls_back_to_raw_path() -> None:
    cal = _live_like_calibrator()
    thin = {"AAA": -0.25, "BBB": -0.15}  # 2 finite < _RECENTER_MIN_FINITE
    ctx_on = _ctx(thin, cal, flag=True)
    ctx_off = _ctx(thin, cal, flag=False)
    ApplyGlobalCalibrationTask().run(ctx_on)
    ApplyGlobalCalibrationTask().run(ctx_off)
    assert _snapshot(ctx_on) == _snapshot(ctx_off)


# ── 7. Kelly μ head recentered consistently ──────────────────────────────────

def test_use_calibrator_mu_head_is_recentered_too() -> None:
    cal = _live_like_calibrator(
        expected_return_label_contract="raw_return_units_required",
    )
    ctx = _ctx(LIVE_SHAPED, cal, flag=True, use_calibrator_mu=True)
    assert ApplyGlobalCalibrationTask().run(ctx) is not False
    center = float(np.median(list(LIVE_SHAPED.values())))
    anchor = cal.neutral_raw
    for c in ctx.candidates:
        x = LIVE_SHAPED[c.ticker] - center + anchor
        assert c.mu == pytest.approx(cal.expected_return(x))
        assert c.mu == pytest.approx(c.expected_return)  # 60d == 60d, no scale
    # The median name's μ is exactly 0 → the signal-direction gate (BL-4,
    # μ>0) refuses it; names above center clear it. Downstream thresholds
    # (mu_floor 0.03 etc.) are untouched by this change.
    by_ticker = {c.ticker: c for c in ctx.candidates}
    assert by_ticker["CCC"].mu == pytest.approx(0.0, abs=1e-12)
    assert math.isfinite(by_ticker["EEE"].mu) and by_ticker["EEE"].mu > 0.0
