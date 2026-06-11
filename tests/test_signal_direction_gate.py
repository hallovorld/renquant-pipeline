"""Signal-direction buy gate (2026-06-10): never long a bearish raw signal.

Pins the operator-flagged failure: on 2026-06-10 the system bought 5 names
whose raw panel_score was −0.10..−0.13 because the calibrator extrapolated a
positive μ (+0.034..+0.042). A long must require the model's OWN raw score to
be non-negative, regardless of the calibrated μ.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.rotation import RotationPair
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_joint_actions import JointActionTask
from renquant_pipeline.kernel.pipeline.task_rotation import EmitRotationsTask
from renquant_pipeline.kernel.pipeline.task_selection import (
    SizeAndEmitTask,
    _require_positive_raw_signal_cfg,
)
from renquant_pipeline.kernel.pipeline.task_topup import TopUpHeldTask


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


def test_positive_raw_with_nonpositive_expected_return_blocked():
    cand = _cand("POS_BAD_ER", panel_score=0.05, expected_return=-0.01, mu=-0.01)
    ctx = _ctx([cand], ["POS_BAD_ER"])
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["POS_BAD_ER"] == "nonpositive_expected_return_no_long"


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


def test_rotation_buy_leg_uses_signal_direction_gate():
    bad = _cand("NEG", panel_score=-0.11)
    ctx = _ctx([bad], [])
    ctx.holdings = {
        "OLD": SimpleNamespace(shares=10, entry_date=dt.date(2026, 1, 1), entry_price=90.0)
    }
    ctx.prices = {"NEG": 100.0, "OLD": 100.0}
    ctx.cash = 10_000.0
    ctx.rotations = [
        RotationPair(
            sell_ticker="OLD",
            buy_ticker="NEG",
            sell_score=0.2,
            buy_score=0.7,
            sell_er=-0.02,
            buy_er=0.04,
            horizon_days=60,
            raw_advantage=0.06,
            tax_drag=0.0,
            transaction_cost=0.0,
            net_advantage=0.06,
            threshold=0.0,
            margin_realized=0.06,
        )
    ]

    EmitRotationsTask().run(ctx)

    assert ctx.orders == []
    assert ctx.exits == []
    assert ctx._blocked_by_ticker["NEG"] == "negative_raw_signal_no_long"
    assert ctx.rotations_blocked[0]["reason"] == "negative_raw_signal_no_long"


def test_topup_uses_signal_direction_gate():
    ctx = _ctx([], [])
    ctx.config["ranking"]["kelly_sizing"] = {
        "enabled": True,
        "top_up_threshold": 0.01,
        "topup_conviction_floor": 0.0,
    }
    ctx.holdings = {
        "NEG": SimpleNamespace(
            shares=1,
            panel_score=-0.11,
            expected_return=0.04,
            mu=0.04,
            rank_score=0.9,
            kelly_target_pct=0.50,
        )
    }
    ctx.prices = {"NEG": 100.0}
    ctx.cash = 10_000.0

    TopUpHeldTask().run(ctx)

    assert ctx.orders == []
    assert ctx._blocked_by_ticker["NEG"] == "negative_raw_signal_no_long"


def test_joint_action_buy_and_rotation_menu_use_signal_direction_gate():
    bad = _cand("NEG", panel_score=-0.11)
    ctx = _ctx([bad], [])
    ctx.config["rotation"] = {
        "joint_actions": {"enabled": True, "solver": "greedy"},
        "panel_buy_floor": 0.1,
        "panel_sell_floor": 0.3,
        "max_rotations_per_bar": 1,
    }
    ctx.config["max_positions_per_sector"] = 0
    ctx.config["wash_sale_days"] = 0
    ctx.holdings = {
        "OLD": SimpleNamespace(
            shares=10,
            entry_date=dt.date(2026, 1, 1),
            entry_price=90.0,
            rank_score=0.1,
            expected_return=-0.02,
        )
    }
    ctx.prices = {"NEG": 100.0, "OLD": 100.0}
    ctx.cash = 10_000.0

    JointActionTask().run(ctx)

    assert ctx.orders == []
    assert ctx.rotations == []
    assert ctx._blocked_by_ticker["NEG"] == "negative_raw_signal_no_long"
