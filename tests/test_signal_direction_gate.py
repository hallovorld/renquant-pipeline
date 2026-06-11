"""Signal-direction buy gate (2026-06-10): never long a bearish raw signal.

Pins the operator-flagged failure: on 2026-06-10 the system bought 5 names
whose raw panel_score was −0.10..−0.13 because the calibrator extrapolated a
positive μ (+0.034..+0.042). A long must require the model's OWN raw score to
be non-negative, regardless of the calibrated μ.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import (
    SizeAndEmitTask,
    _require_positive_raw_signal_cfg,
)


def _cand(ticker, panel_score, *, expected_return=0.04, mu=0.04, sigma=0.2):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail={}, expected_return=expected_return, expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _ctx(ranked, selected, **overrides):
    cfg = {
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.10,
                                        "cash_reserve_pct": 0.0,
                                        "max_concurrent_positions": 8}},
        "ranking": {"panel_scoring": {"enabled": True, "sizing": {}, "sigma_sizing": {}},
                    "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    if "config" in overrides:
        cfg = overrides.pop("config")
    values = {
        "config": cfg, "today": dt.date(2026, 6, 10), "regime": "BULL_CALM",
        "confidence": 0.69, "bear_only": False, "portfolio_value": 10_000.0,
        "cash": 10_000.0, "prices": {c.ticker: 100.0 for c in ranked},
        "ranked": ranked, "models": {},
    }
    values.update(overrides)
    ctx = InferenceContext(**values)
    ctx._selected = selected  # noqa: SLF001
    return ctx


def test_negative_panel_score_blocked_from_long():
    pos = _cand("POS", panel_score=0.05)
    neg = _cand("NEG", panel_score=-0.11)  # bearish raw, but calibrated μ +0.04
    ctx = _ctx([pos, neg], ["POS", "NEG"])
    SizeAndEmitTask().run(ctx)
    bought = [o["ticker"] for o in ctx.orders]
    assert "NEG" not in bought                       # bearish signal not longed
    assert ctx._blocked_by_ticker["NEG"] == "negative_raw_signal_no_long"


def test_positive_panel_score_allowed():
    pos = _cand("POS", panel_score=0.05)
    ctx = _ctx([pos], ["POS"])
    SizeAndEmitTask().run(ctx)
    assert "POS" in [o["ticker"] for o in ctx.orders]  # bullish signal trades


def test_all_negative_universe_buys_nothing():
    # PatchTST-like: every raw score negative → no new long at all.
    cands = [_cand(t, panel_score=p) for t, p in
             [("SPOT", -0.11), ("HON", -0.12), ("LLY", -0.13)]]
    ctx = _ctx(cands, ["SPOT", "HON", "LLY"])
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    for t in ("SPOT", "HON", "LLY"):
        assert ctx._blocked_by_ticker[t] == "negative_raw_signal_no_long"


def test_gate_is_on_by_default_and_opt_out():
    assert _require_positive_raw_signal_cfg({}) is False
    assert _require_positive_raw_signal_cfg(
        {"ranking": {"panel_scoring": {"enabled": True, "require_positive_raw_signal_for_buy": False}}}
    ) is False
    assert _require_positive_raw_signal_cfg(
        {"ranking": {"panel_scoring": {"enabled": True}}}
    ) is True


def test_opt_out_allows_negative_signal_long():
    neg = _cand("NEG", panel_score=-0.11)
    cfg = {
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.10,
                                        "cash_reserve_pct": 0.0,
                                        "max_concurrent_positions": 8}},
        "ranking": {"panel_scoring": {"enabled": True, "sizing": {}, "sigma_sizing": {},
                                      "require_positive_raw_signal_for_buy": False},
                    "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    ctx = _ctx([neg], ["NEG"], config=cfg)
    SizeAndEmitTask().run(ctx)
    # gate disabled → the bearish name is NOT blocked by the signal-direction
    # gate (it proceeds to sizing; whether it ultimately trades is up to cash)
    blocked = getattr(ctx, "_blocked_by_ticker", {}) or {}
    assert blocked.get("NEG") != "negative_raw_signal_no_long"


def test_disabled_panel_scoring_does_not_apply_raw_signal_gate():
    cand = _cand("AAA", panel_score=None, expected_return=0.04, mu=0.04)
    cfg = {
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.10,
                                        "cash_reserve_pct": 0.0,
                                        "max_concurrent_positions": 8}},
        "ranking": {"panel_scoring": {"enabled": False, "sizing": {}, "sigma_sizing": {}},
                    "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    ctx = _ctx([cand], ["AAA"], config=cfg)
    SizeAndEmitTask().run(ctx)
    assert "AAA" in [o["ticker"] for o in ctx.orders]
