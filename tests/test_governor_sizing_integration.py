"""Deployment Governor pipeline integration (RFC 2026-07-09 D2–D4, flag OFF).

Contract pinned here (same discipline as the A-3 one-share-floor tests):

  * ``deployment_governor`` absent / ``enabled: false`` / malformed ⇒
    BYTE-IDENTICAL ``SizeAndEmitTask`` behaviour — same orders, same
    block reasons, no new order fields.
  * enabled ⇒ the Governor/allocator own the sizing decision over the
    ALREADY-ADMITTED slate + held book; integer execution is greedy
    whole-share rounding in conviction order with a residual-cash
    second pass; exit legs from weight deltas are emitted only when the
    post-cost (tax drag + linear cost) improvement is positive; min-hold
    / wash-sale act as no-sell masks.
  * Fail-closed: a model fault makes the Governor emit NO target and
    the legacy path runs unchanged.
"""
from __future__ import annotations

import datetime as dt

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.exits import HoldingState
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask

TODAY = dt.date(2026, 7, 9)
PV = 10_000.0
CASH = 10_000.0
REGIME_CAP_PCT = 0.12

GOV_SCHEMA = {
    "enabled": True,
    "e_ceil_by_regime": {"BULL_CALM": 0.95, "BULL_VOLATILE": 0.7,
                         "CHOPPY": 0.6, "BEAR": 0.35},
    "hysteresis_band": 0.05,
    "kelly_fraction": 0.3,
    "mu_shrinkage": 0.0,
    "top_k": 8,
    "max_step_per_session": 0.15,
}


def _cand(ticker, panel_score=0.5, *, mu=0.04, sigma=0.2,
          expected_return=0.04):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=expected_return,
        expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _held(entry_price, *, shares, mu, sigma=0.2, days_held=100):
    return HoldingState(
        entry_price=entry_price,
        entry_date=TODAY - dt.timedelta(days=days_held),
        high_watermark=entry_price,
        shares=shares,
        mu=mu,
        sigma=sigma,
    )


def _config(*, governor=None, **top):
    cfg = {
        "regime_params": {"BULL_CALM": {
            "max_position_pct": REGIME_CAP_PCT,
            "cash_reserve_pct": 0.0,
            "max_concurrent_positions": 8,
        }},
        "ranking": {"panel_scoring": {
            "enabled": True,
            "sizing": {"enabled": True, "floor": 0.0, "ceiling": 1.0,
                       "min_mult": 0.5},
            "sigma_sizing": {},
        }, "kelly_sizing": {"enabled": False}},
        "regime": {},
        "wash_sale_days": 0,
        "min_hold_days": 0,
    }
    if governor is not None:
        cfg["deployment_governor"] = governor
    cfg.update(top)
    return cfg


def _gov(**overrides):
    gov = dict(GOV_SCHEMA)
    gov.update(overrides)
    return gov


def _ctx(ranked, selected, config, *, cash=CASH, pv=PV, prices=None,
         holdings=None, **overrides):
    values = {
        "config": config, "today": TODAY, "regime": "BULL_CALM",
        "confidence": 1.0, "bear_only": False, "portfolio_value": pv,
        "cash": cash,
        "prices": prices or {c.ticker: 50.0 for c in ranked},
        "ranked": ranked, "models": {},
        "holdings": holdings or {},
    }
    values.update(overrides)
    ctx = InferenceContext(**values)
    ctx._selected = selected  # noqa: SLF001
    return ctx


def _run(ctx):
    SizeAndEmitTask().run(ctx)
    return ctx


def _order_map(ctx):
    return {o["ticker"]: (o["shares"], o["invest"]) for o in ctx.orders}


def _blocked(ctx):
    return dict(getattr(ctx, "_blocked_by_ticker", {}) or {})


# ═════════════════════════════════════════════════════════════════════
#  Flag OFF: byte-identical regression (same contract as A-3's tests)
# ═════════════════════════════════════════════════════════════════════

def _legacy_panel():
    return [
        _cand("OXY", panel_score=0.6),                 # normal, cheap
        _cand("BLK", panel_score=0.001),               # rounds to 0 shares
        _cand("NEG", panel_score=-0.11),               # signal-gate blocked
    ]


_LEGACY_PRICES = {"OXY": 48.0, "BLK": 11_000.0, "NEG": 30.0}
_LEGACY_SELECTED = ["OXY", "BLK", "NEG"]


def _run_legacy_panel(config):
    ctx = _ctx(_legacy_panel(), list(_LEGACY_SELECTED), config,
               prices=dict(_LEGACY_PRICES))
    return _run(ctx)


def test_flag_absent_vs_enabled_false_byte_identical():
    base = _run_legacy_panel(_config())
    off = _run_legacy_panel(_config(governor={"enabled": False}))
    assert off.orders == base.orders                   # full dict equality
    assert _blocked(off) == _blocked(base)
    assert off.counters == base.counters


def test_flag_absent_vs_block_without_enabled_key_byte_identical():
    base = _run_legacy_panel(_config())
    off = _run_legacy_panel(_config(governor=dict(GOV_SCHEMA, enabled=False)))
    assert off.orders == base.orders
    assert _blocked(off) == _blocked(base)


def test_malformed_governor_block_treated_as_off():
    base = _run_legacy_panel(_config())
    off = _run_legacy_panel(_config(governor="oops-not-a-dict"))
    assert off.orders == base.orders
    assert _blocked(off) == _blocked(base)


def test_flag_off_orders_carry_no_governor_fields():
    ctx = _run_legacy_panel(_config(governor={"enabled": False}))
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert "sizing_mode" not in order
    assert not any("governor" in str(k) for k in order)
    assert not any("governor" in str(k) for k in order["decision_inputs"])
    assert "governor_sessions" not in ctx.counters
    assert not hasattr(ctx, "_deployment_governor")


def test_bear_only_keeps_legacy_path_even_when_enabled():
    def _bear_ctx(config):
        ctx = _ctx([_cand("SPY")], ["SPY"], config, bear_only=True,
                   prices={"SPY": 500.0})
        return _run(ctx)
    base = _bear_ctx(_config())
    on = _bear_ctx(_config(governor=_gov()))
    assert on.orders == base.orders
    assert _blocked(on) == _blocked(base)
    assert "governor_sessions" not in on.counters


def test_buy_blocked_still_suppresses_before_governor():
    ctx = _ctx([_cand("AAA")], ["AAA"], _config(governor=_gov()),
               buy_blocked=True)
    _run(ctx)
    assert ctx.orders == []
    assert _blocked(ctx)["AAA"] == "buy_blocked"
    assert "governor_sessions" not in ctx.counters


# ═════════════════════════════════════════════════════════════════════
#  Flag ON: Governor-owned sizing
# ═════════════════════════════════════════════════════════════════════

def test_enabled_sizes_to_allocator_target_weights():
    # raws: A 0.3→cap 0.12, B 0.15→cap 0.12, C 0.06 ⇒ E*=0.30 (< ceil).
    ranked = [
        _cand("A", mu=0.04), _cand("B", mu=0.02), _cand("C", mu=0.008),
    ]
    prices = {"A": 50.0, "B": 30.0, "C": 20.0}
    cfg = _config(governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0))
    ctx = _run(_ctx(ranked, ["A", "B", "C"], cfg, prices=prices))
    orders = _order_map(ctx)
    assert orders == {
        "A": (24, pytest.approx(1200.0)),   # 0.12 × 10k / 50
        "B": (40, pytest.approx(1200.0)),   # 0.12 × 10k / 30
        "C": (30, pytest.approx(600.0)),    # 0.06 × 10k / 20
    }
    for o in ctx.orders:
        assert o["sizing_mode"] == "deployment_governor"
        assert o["order_type"] == "NEW_BUY"
        assert o["decision_inputs"]["governor_e_target"] == pytest.approx(0.30)
    ledger = ctx._deployment_governor  # noqa: SLF001
    assert ledger["e_target"] == pytest.approx(0.30)
    assert ledger["e_final"] == pytest.approx(0.30)
    assert ledger["residual"] == pytest.approx(0.0)
    assert ctx.counters["governor_sessions"] == 1


def test_step_limit_bounds_first_session_deployment():
    ranked = [_cand("A", mu=0.04), _cand("B", mu=0.02)]
    cfg = _config(governor=_gov(hysteresis_band=0.0))   # max_step 0.15
    ctx = _run(_ctx(ranked, ["A", "B"], cfg,
                    prices={"A": 50.0, "B": 30.0}))
    # E_raw = 0.24, E_current = 0 ⇒ E* clamped to 0.15; targets scale
    # by 0.15/0.24 ⇒ A=B=0.075 ⇒ $750 each.
    orders = _order_map(ctx)
    assert orders["A"] == (15, pytest.approx(750.0))
    assert orders["B"] == (25, pytest.approx(750.0))
    assert ctx._deployment_governor["e_target"] == pytest.approx(0.15)  # noqa: SLF001


def test_residual_cash_second_pass_reoffers_leftover():
    # target 0.0975 → $975 @ $300 ⇒ main pass 3 shares ($900); residual
    # pass re-offers leftover cash: +1 share → $1200 = 0.12 ≤ cap.
    ranked = [_cand("A", mu=0.013)]
    cfg = _config(governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0))
    ctx = _run(_ctx(ranked, ["A"], cfg, prices={"A": 300.0}))
    orders = _order_map(ctx)
    assert orders["A"] == (4, pytest.approx(1200.0))
    assert ctx.counters["governor_residual_reoffers"] == 1
    # Cap invariant on REALIZED weight: never above per-name cap.
    assert orders["A"][1] / PV <= REGIME_CAP_PCT + 1e-6


def test_residual_pass_respects_per_name_cap():
    # Target == cap (0.12 → $1200) @ $700: main pass 1 share ($700);
    # +1 share would be 0.14 > cap ⇒ residual pass must NOT round up.
    ranked = [_cand("A", mu=0.04)]
    cfg = _config(governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0))
    ctx = _run(_ctx(ranked, ["A"], cfg, prices={"A": 700.0}))
    orders = _order_map(ctx)
    assert orders["A"] == (1, pytest.approx(700.0))
    assert "governor_residual_reoffers" not in ctx.counters


def test_no_crowd_out_of_higher_conviction():
    # Tight cash: HI (higher raw) must be funded FIRST; LO only gets
    # what is genuinely left over.
    ranked = [_cand("HI", mu=0.04), _cand("LO", mu=0.013)]
    cfg = _config(governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0))
    ctx = _run(_ctx(ranked, ["HI", "LO"], cfg, cash=700.0,
                    prices={"HI": 600.0, "LO": 90.0}))
    orders = _order_map(ctx)
    assert orders["HI"] == (1, pytest.approx(600.0))    # funded first
    assert orders["LO"] == (1, pytest.approx(90.0))     # leftover only
    assert [o["ticker"] for o in ctx.orders] == ["HI", "LO"]
    assert sum(o["invest"] for o in ctx.orders) <= 700.0


# ── Fail-closed: model fault ⇒ legacy path, byte-identical ────────────────

def test_model_fault_falls_back_to_legacy_byte_identical():
    def _panel():
        return [_cand("OXY", panel_score=0.6, mu=None, sigma=None)]
    base = _run(_ctx(_panel(), ["OXY"], _config(), prices={"OXY": 48.0}))
    on = _run(_ctx(_panel(), ["OXY"], _config(governor=_gov()),
                   prices={"OXY": 48.0}))
    assert on.counters.pop("governor_fault_fallback_legacy") == 1
    assert on.orders == base.orders                     # legacy sizing ran
    assert _blocked(on) == _blocked(base)
    assert on._deployment_governor == {"fault": "model_fault"}  # noqa: SLF001


def test_invalid_portfolio_value_falls_back_to_legacy():
    ctx = _ctx([_cand("A")], ["A"], _config(governor=_gov()), pv=0.0)
    _run(ctx)
    assert ctx.counters.get("governor_fault_fallback_legacy") == 1


def test_unmapped_regime_falls_back_to_legacy():
    cfg = _config(governor=_gov(e_ceil_by_regime={"BEAR": 0.35}))
    cfg["regime_params"]["BULL_CALM"]["max_position_pct"] = REGIME_CAP_PCT
    base = _run(_ctx([_cand("OXY")], ["OXY"], _config(),
                     prices={"OXY": 48.0}))
    on = _run(_ctx([_cand("OXY")], ["OXY"], cfg, prices={"OXY": 48.0}))
    assert on.counters.pop("governor_fault_fallback_legacy") == 1
    assert on.orders == base.orders


# ── Hysteresis hold ⇒ no reallocation ─────────────────────────────────────

def test_hysteresis_hold_emits_no_orders():
    held = {"H": _held(100.0, shares=10, mu=0.014)}     # w=0.10, raw 0.105
    ranked = [_cand("C", mu=0.001)]                     # raw 0.0015
    cfg = _config(governor=_gov())                      # band 0.05
    ctx = _run(_ctx(ranked, ["C"], cfg, cash=1_000.0,
                    prices={"H": 100.0, "C": 50.0}, holdings=held))
    assert ctx.orders == []
    assert ctx.exits == []
    assert _blocked(ctx)["C"] == "governor_hysteresis_hold"
    assert ctx.counters["governor_hysteresis_holds"] == 1
    ledger = ctx._deployment_governor  # noqa: SLF001
    assert ledger["hysteresis_held"] is True
    assert ledger["e_target"] == pytest.approx(0.10)


# ── Exit legs from weight deltas (pair post-cost gate, RFC §1.3) ──────────

def _pair_setup(*, entry_price, min_hold_days=0, days_held=100):
    held = {"D": _held(entry_price, shares=10, mu=0.001,
                       days_held=days_held)}
    ranked = [_cand("A", mu=0.06)]                      # raw 0.45 → cap 0.12
    cfg = _config(
        governor=_gov(hysteresis_band=0.01, max_step_per_session=1.0),
        min_hold_days=min_hold_days,
    )
    return _ctx(ranked, ["A"], cfg, cash=0.0,
                prices={"D": 100.0, "A": 100.0}, holdings=held)


def test_pair_sell_emitted_when_post_cost_positive():
    # D flat (no gain → no tax): improvement ≈ (0.06−0.001)·$900 > costs.
    ctx = _run(_pair_setup(entry_price=100.0))
    assert len(ctx.exits) == 1
    ticker, sig = ctx.exits[0]
    assert ticker == "D"
    assert sig.exit_type == "governor_rebalance"
    assert sig.quantity == pytest.approx(9.0)           # trims 0.10 → 0.0075
    assert ctx.counters["governor_pair_sells"] == 1
    # Proceeds redeployed into the unfilled high-conviction buy.
    orders = _order_map(ctx)
    assert orders["A"] == (9, pytest.approx(900.0))


def test_pair_sell_rejected_when_tax_drag_kills_improvement():
    # Entry $10 → +900% unrealized, short-term 50% tax ⇒ post-cost < 0.
    ctx = _run(_pair_setup(entry_price=10.0))
    assert ctx.exits == []
    assert ctx.counters["governor_pair_rejected_post_cost"] == 1
    assert _order_map(ctx) == {}                        # no cash freed
    assert _blocked(ctx)["A"] == "governor_unfilled_target"


def test_min_hold_no_sell_mask_blocks_pair_sell():
    ctx = _run(_pair_setup(entry_price=100.0, min_hold_days=5, days_held=2))
    assert ctx.exits == []                              # masked — never sold
    assert "governor_pair_sells" not in ctx.counters


def test_wash_sale_loss_lot_is_no_sell_masked():
    # Held at a LOSS, bought inside the wash-sale window ⇒ §1091 no-sell.
    held = {"D": _held(120.0, shares=10, mu=0.001, days_held=10)}
    ranked = [_cand("A", mu=0.06)]
    cfg = _config(
        governor=_gov(hysteresis_band=0.01, max_step_per_session=1.0),
        wash_sale_days=30,
    )
    ctx = _run(_ctx(ranked, ["A"], cfg, cash=0.0,
                    prices={"D": 100.0, "A": 100.0}, holdings=held))
    assert ctx.exits == []


# ── Weak slate: low E* with ledger stats, never a fault ───────────────────

def test_weak_slate_stamps_stats_and_emits_nothing():
    ranked = [_cand("A", mu=-0.01)]                     # moments OK, raw 0
    cfg = _config(governor=_gov())
    ctx = _run(_ctx(ranked, ["A"], cfg, prices={"A": 50.0}))
    assert ctx.orders == []
    assert ctx.counters["governor_sessions"] == 1
    assert "governor_fault_fallback_legacy" not in ctx.counters
    ledger = ctx._deployment_governor  # noqa: SLF001
    assert ledger["slate_stats"]["weak_slate"] is True
    assert ledger["slate_stats"]["admitted_count"] == 0


# ── Admission chain untouched: signal-direction gate still applies ────────

def test_signal_direction_gate_applies_inside_governor_path():
    ranked = [_cand("NEG", panel_score=-0.11, mu=0.05)]
    cfg = _config(governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0))
    ctx = _run(_ctx(ranked, ["NEG"], cfg, prices={"NEG": 50.0}))
    assert ctx.orders == []
    assert _blocked(ctx)["NEG"] == "negative_raw_signal_no_long"


def test_wash_sale_no_buy_mask_blocks_topup_of_held_name():
    # Held name recently sold (wash-sale window): weight may not increase.
    held = {"H": _held(100.0, shares=5, mu=0.04)}       # w=0.05, raw 0.3→cap
    cfg = _config(
        governor=_gov(hysteresis_band=0.0, max_step_per_session=1.0),
        wash_sale_days=30,
    )
    ctx = _ctx([], [], cfg, cash=5_000.0, prices={"H": 100.0}, holdings=held,
               last_sell_dates={"H": TODAY - dt.timedelta(days=5)})
    _run(ctx)
    assert _order_map(ctx) == {}                        # no top-up emitted
    assert ctx.counters["governor_sessions"] == 1
