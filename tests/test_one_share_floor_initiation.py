"""S6 A-3 (2026-07-02): one-share floor for high-price INITIATIONS.

Pins the 2026-07-01 OXY forensics artifact: the multiplicative sizing stack
(Kelly × conviction × σ-mult × PV) compounds a target notional below ONE
share of a high-price name (BLK target $324 < 1 share ~$1.1k), the whole-share
sizer returns 0 shares, and the name is dropped as `size_insufficient_cash`
— so selection structurally drifts toward LOW-price names (OXY $48 partially
won *because* it is cheap).

With `sizing.one_share_floor_enabled: true` (default OFF — inert until
strategy-104 defines it), a candidate that zeroes out ONLY because of
whole-share rounding rounds UP to exactly one share iff
  (a) one share ≤ regime max_position_pct × PV,
  (b) one share ≤ investable headroom after cash reservations,
  (c) the name already passed EVERY admission gate (sizing-only change).
Every round-up is stamped with a dedicated ledger reason field
(`size_floor_reason = "one_share_floor_round_up"`).

Flag absent ⇒ byte-identical behaviour (regression-pinned below).
References: capability program §1.2 A-3; RS-2 lane-A timing memo (2026-07-02).
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask

# BLK-like fixture (2026-07-01 run numbers): PV $10,806, cash $8,140,
# BULL_CALM max_position_pct 12%, BLK 1 share ≈ $1,100.
PV = 10_806.0
CASH = 8_140.0
BLK_PRICE = 1_100.0
REGIME_CAP_PCT = 0.12  # 12% × $10,806 = $1,296.72 ≥ 1 share of BLK


def _cand(ticker, panel_score=0.001, *, expected_return=0.04, mu=0.04, sigma=0.2):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=expected_return,
        expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _config(*, one_share_floor=None, cash_reserve_pct=0.0):
    cfg = {
        "regime_params": {"BULL_CALM": {
            "max_position_pct": REGIME_CAP_PCT,
            "cash_reserve_pct": cash_reserve_pct,
            "max_concurrent_positions": 8,
        }},
        # Conviction sizing ON (floor 0 → min_mult 0.5) so the compounded
        # target (0.12 × ~0.50 ≈ 6% ≈ $649) lands BELOW one BLK share —
        # the measured selection-by-share-price artifact.
        "ranking": {"panel_scoring": {
            "enabled": True,
            "sizing": {"enabled": True, "floor": 0.0, "ceiling": 1.0,
                       "min_mult": 0.5},
            "sigma_sizing": {},
        }, "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    if one_share_floor is not None:
        cfg["sizing"] = one_share_floor
    return cfg


def _ctx(ranked, selected, config, *, cash=CASH, pv=PV, prices=None, **overrides):
    values = {
        "config": config, "today": dt.date(2026, 7, 1), "regime": "BULL_CALM",
        "confidence": 1.0, "bear_only": False, "portfolio_value": pv,
        "cash": cash,
        "prices": prices or {c.ticker: BLK_PRICE for c in ranked},
        "ranked": ranked, "models": {},
    }
    values.update(overrides)
    ctx = InferenceContext(**values)
    ctx._selected = selected  # noqa: SLF001
    return ctx


# ── Flag OFF (default): byte-identical legacy behaviour ───────────────────────

def test_flag_absent_high_price_name_still_dropped():
    """Regression: no `sizing` config section ⇒ BLK-class drop unchanged."""
    blk = _cand("BLK")
    ctx = _ctx([blk], ["BLK"], _config())
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"
    assert "one_share_floor_roundups" not in ctx.counters


def test_flag_explicitly_false_identical_to_absent():
    blk = _cand("BLK")
    ctx = _ctx([blk], ["BLK"],
               _config(one_share_floor={"one_share_floor_enabled": False}))
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"


def test_flag_off_cheap_name_order_carries_no_floor_fields():
    """Flag-off orders must not gain ANY new field (byte-identical contract)."""
    oxy = _cand("OXY")
    ctx = _ctx([oxy], ["OXY"], _config(), prices={"OXY": 48.0})
    SizeAndEmitTask().run(ctx)
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert "size_floor_reason" not in order
    assert "one_share_floor_applied" not in order["decision_inputs"]


def test_malformed_sizing_root_treated_as_off():
    """Safe default: a non-dict `sizing` value never crashes, floor stays off."""
    blk = _cand("BLK")
    cfg = _config()
    cfg["sizing"] = "oops-not-a-dict"
    ctx = _ctx([blk], ["BLK"], cfg)
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"


# ── Flag ON: the A-3 contract ─────────────────────────────────────────────────

def _flag_on_config(**kwargs):
    return _config(one_share_floor={"one_share_floor_enabled": True}, **kwargs)


def test_blk_like_rounds_up_to_exactly_one_share():
    """Target ~$649 < 1 share $1,100 ≤ 12% cap ($1,296.72) ≤ headroom ⇒ 1 share."""
    blk = _cand("BLK")
    ctx = _ctx([blk], ["BLK"], _flag_on_config())
    SizeAndEmitTask().run(ctx)
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["ticker"] == "BLK"
    assert order["shares"] == 1
    assert order["invest"] == BLK_PRICE
    assert "BLK" not in (getattr(ctx, "_blocked_by_ticker", {}) or {})
    # Dedicated ledger reason field + counter — every round-up is auditable.
    assert order["size_floor_reason"] == "one_share_floor_round_up"
    assert order["decision_inputs"]["one_share_floor_applied"] is True
    assert ctx.counters["one_share_floor_roundups"] == 1


def test_kelly_path_blk_target_324_rounds_up():
    """Production-shaped repro: Kelly target 3% × $10,806 = $324 < 1 share."""
    blk = _cand("BLK")
    blk.kelly_target_pct = 0.03  # stamped upstream by ApplyKellySizingTask
    cfg = _flag_on_config()
    cfg["ranking"]["kelly_sizing"] = {"enabled": True,
                                      "disable_extra_multipliers": True}
    ctx = _ctx([blk], ["BLK"], cfg)
    SizeAndEmitTask().run(ctx)
    assert [o["shares"] for o in ctx.orders] == [1]
    assert ctx.orders[0]["size_floor_reason"] == "one_share_floor_round_up"


def test_one_share_above_regime_cap_still_dropped():
    """(a) violated: 1 share $5,000 > 12% × PV = $1,296.72 ⇒ drop (cash ample)."""
    bkng = _cand("BKNG")
    ctx = _ctx([bkng], ["BKNG"], _flag_on_config(),
               prices={"BKNG": 5_000.0})  # cash $8,140 could afford it
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BKNG"] == "size_insufficient_cash"
    assert "one_share_floor_roundups" not in ctx.counters


def test_insufficient_cash_headroom_still_dropped():
    """(b) violated: 1 share $1,100 > remaining cash $900 ⇒ drop."""
    blk = _cand("BLK")
    ctx = _ctx([blk], ["BLK"], _flag_on_config(), cash=900.0)
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"


def test_headroom_is_after_cash_reservation():
    """(b) uses investable AFTER reservations: $1,500 − 5%×PV($540.30) < $1,100."""
    blk = _cand("BLK")
    ctx = _ctx([blk], ["BLK"], _flag_on_config(cash_reserve_pct=0.05),
               cash=1_500.0)
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"


def test_headroom_checked_against_remaining_cash_not_starting_cash():
    """Cumulative fill: a prior buy consumes cash; the floor must not overdraw."""
    oxy = _cand("OXY", panel_score=0.9)   # ranked first, buys ~$649 of shares
    blk = _cand("BLK", panel_score=0.001)
    ctx = _ctx([oxy, blk], ["OXY", "BLK"], _flag_on_config(),
               cash=1_500.0, prices={"OXY": 48.0, "BLK": BLK_PRICE})
    SizeAndEmitTask().run(ctx)
    bought = {o["ticker"] for o in ctx.orders}
    assert "OXY" in bought
    # OXY spent > $400, remaining < $1,100 ⇒ BLK one-share floor ineligible.
    assert "BLK" not in bought
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"
    spent = sum(o["invest"] for o in ctx.orders)
    assert spent <= 1_500.0


def test_admission_failed_name_is_never_floor_sized():
    """(c): the floor changes SIZING only — a gate-blocked name never trades."""
    neg = _cand("NEG", panel_score=-0.11)  # signal-direction gate blocks longs
    ctx = _ctx([neg], ["NEG"], _flag_on_config())
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["NEG"] == "negative_raw_signal_no_long"
    assert "one_share_floor_roundups" not in ctx.counters


def test_flag_on_normal_sized_name_untouched():
    """Flag-on must not perturb names the whole-share sizer already handles."""
    oxy = _cand("OXY")
    ctx = _ctx([oxy], ["OXY"], _flag_on_config(), prices={"OXY": 48.0})
    SizeAndEmitTask().run(ctx)
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["shares"] > 1                      # normal whole-share sizing
    assert "size_floor_reason" not in order
    assert "one_share_floor_applied" not in order["decision_inputs"]
    assert "one_share_floor_roundups" not in ctx.counters


def test_bear_defensive_path_keeps_legacy_drop():
    """BEAR defensive slots (override_pct) are out of scope for A-3."""
    spy = _cand("SPY")
    cfg = _flag_on_config()
    cfg["bear_defensive_pct"] = 0.15
    cfg["bear_defensive_slots"] = 1
    ctx = _ctx([spy], ["SPY"], cfg, bear_only=True,
               prices={"SPY": 5_000.0})  # 1 share > 15% defensive slot
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["SPY"] == "size_insufficient_cash"
    assert "one_share_floor_roundups" not in ctx.counters
