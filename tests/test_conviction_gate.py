"""ConvictionGateTask: economic-conviction floor on the calibrated surface.

2026-06-22 operator review: a near-break-even raw score gets a high rank
percentile but a calibrated expected return mu ~= 0. The rank_score percentile
floor (VetoWeakBuysTask) can't see it; an expected-return floor can. Models the
live case — NFLX raw -0.26 sits just above the XGB neutral -0.27 -> mu ~= 0,
while PANW raw +0.057 -> mu +6%.
"""
from __future__ import annotations

from types import SimpleNamespace

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import ConvictionGateTask


def _c(ticker: str, mu: float, raw: float) -> SimpleNamespace:
    return SimpleNamespace(ticker=ticker, expected_return=mu, panel_score=raw)


def _ctx(cands, **gate) -> SimpleNamespace:
    cfg = {"conviction_gate": {"enabled": True, **gate}}
    return SimpleNamespace(
        candidates=list(cands),
        config={"ranking": {"panel_scoring": cfg}},
        counters={},
    )


def test_disabled_is_noop() -> None:
    cands = [_c("A", 0.001, -0.26), _c("B", 0.06, 0.05)]
    ctx = SimpleNamespace(
        candidates=list(cands),
        config={"ranking": {"panel_scoring": {}}},
        counters={},
    )
    assert ConvictionGateTask().run(ctx) is None
    assert len(ctx.candidates) == 2  # nothing dropped when gate absent


def test_enabled_but_no_floors_is_noop() -> None:
    ctx = _ctx([_c("A", 0.001, -0.26)])  # enabled but neither floor set
    assert ConvictionGateTask().run(ctx) is None
    assert len(ctx.candidates) == 1


def test_mu_floor_drops_near_breakeven_noise() -> None:
    # PANW +6%, CSCO +4.2% clear a 3% floor; NFLX ~1%, ZM 1.5% do not.
    cands = [
        _c("PANW", 0.060, 0.057),
        _c("CSCO", 0.042, -0.047),
        _c("NFLX", 0.0096, -0.260),
        _c("ZM", 0.015, -0.204),
    ]
    ctx = _ctx(cands, mu_floor=0.03)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "CSCO"}
    assert ctx._blocked_by_ticker["NFLX"] == "conviction:mu_below_floor"
    assert ctx._blocked_by_ticker["ZM"] == "conviction:mu_below_floor"
    assert ctx.counters["conviction_vetoed"] == 2


def test_min_raw_enforces_literal_raw_positive() -> None:
    cands = [
        _c("PANW", 0.060, 0.057),
        _c("ASML", 0.080, 0.137),
        _c("CSCO", 0.042, -0.047),
        _c("NFLX", 0.0096, -0.260),
    ]
    ctx = _ctx(cands, min_raw_panel_score=0.0)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "ASML"}
    assert ctx._blocked_by_ticker["CSCO"] == "conviction:raw_not_above_floor"


def test_mu_and_raw_combined() -> None:
    # CSCO clears mu (4.2% > 3%) but fails raw>0 -> dropped; PANW clears both.
    cands = [_c("PANW", 0.060, 0.057), _c("CSCO", 0.042, -0.047)]
    ctx = _ctx(cands, mu_floor=0.03, min_raw_panel_score=0.0)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW"}
    assert ctx._blocked_by_ticker["CSCO"] == "conviction:raw_not_above_floor"


def test_nan_mu_is_dropped() -> None:
    cands = [_c("OK", 0.05, 0.05), _c("NANMU", float("nan"), 0.05)]
    ctx = _ctx(cands, mu_floor=0.03)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"OK"}
    assert ctx._blocked_by_ticker["NANMU"] == "conviction:mu_nan"


def test_empty_candidates_is_safe() -> None:
    ctx = _ctx([], mu_floor=0.03)
    assert ConvictionGateTask().run(ctx) is None
