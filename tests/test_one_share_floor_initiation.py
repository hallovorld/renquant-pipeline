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


# ── Round-2 (codex review): intended-notional contract, edge cases ───────────
#
# Intended-notional contract for `SizeAndEmitTask`'s one-share floor, made
# explicit (previously only implicit in the eligibility check):
#
#   Given a target notional `max_pct * PV` and a share price `price`:
#     (a) NORMAL   — max_pct > 0 and target_notional rounds to >= 1 whole
#                    share at `price` ⇒ unaffected by this PR at any flag
#                    setting; whole-share sizing already handles it.
#     (b) RESCUED  — max_pct > 0 (a genuine, positive, model-derived
#                    target) BUT target_notional rounds to 0 whole shares
#                    at `price`, AND flag ON, AND price fits both the
#                    regime cap and investable headroom ⇒ round UP to
#                    exactly 1 share. This is the ONLY case this PR changes.
#     (c) ZERO     — max_pct <= 0 (conviction_multiplier / sigma_multiplier
#                    / kelly_target_pct legitimately computed a ZERO
#                    target -- "the model says invest nothing here", not a
#                    rounding artifact) ⇒ MUST NEVER be floor-rescued,
#                    regardless of flag setting or price. Blocked the same
#                    way as before this PR (Kelly path: "kelly_zero:
#                    capped_zero" before compute_position_size is even
#                    called; legacy path: falls through to the existing
#                    "size_insufficient_cash" block, now correctly excluded
#                    from the floor by the `max_pct > 0` eligibility guard).
#     (d) FLAG OFF — byte-identical to pre-PR behaviour in every regime,
#                    including (b): a flag-off RESCUED-shaped candidate is
#                    dropped exactly as it always was.
#
# max_pct can never be NEGATIVE by construction: `kelly_target_pct()`
# (kernel/kelly.py) returns `max(0.0, min(...))`, and both
# `conviction_multiplier()` / `sigma_multiplier()` (kernel/sizing.py) are
# documented to return values clipped into `[min_mult, 1.0]` /
# `[floor, ceiling]` -- non-negative under any config where those bounds
# are themselves non-negative (the only supported convention; a
# deliberately-negative min_mult/floor is a pre-existing, out-of-scope
# config-validation gap shared by every consumer of these two functions,
# not something this PR introduces or could plausibly worsen).

def test_zero_kelly_target_never_floor_sized():
    """(c) Kelly path: kelly_target_pct=0 is blocked BEFORE the floor logic
    even runs -- it never reaches compute_position_size, let alone the
    floor-eligibility check. Regression guard for the Kelly branch's
    existing `if max_pct <= 0: continue`."""
    blk = _cand("BLK")
    blk.kelly_target_pct = 0.0
    cfg = _flag_on_config()
    cfg["ranking"]["kelly_sizing"] = {"enabled": True,
                                      "disable_extra_multipliers": True}
    ctx = _ctx([blk], ["BLK"], cfg)
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "kelly_zero:capped_zero"
    assert "one_share_floor_roundups" not in ctx.counters


def test_zero_legacy_conviction_target_never_floor_sized():
    """(c) Legacy (non-Kelly) path regression: min_mult=0.0 config + a
    candidate at/below the conviction floor computes conv=0.0 exactly ->
    max_pct=0 -- a genuine "invest nothing" decision, not a rounds-to-zero
    price artifact. Pre-fix this WAS wrongly floor-rescued to 1 share
    whenever price fit the regime cap + investable cash (confirmed
    reproducible: BLK @ $1,100, conviction=0.0, floor=0.5, min_mult=0.0 ->
    bought anyway). The `max_pct > 0` eligibility guard fixes this."""
    blk = _cand("BLK", panel_score=0.01)  # positive: passes signal-direction gate
    cfg = _flag_on_config()
    cfg["ranking"]["panel_scoring"]["sizing"] = {
        "enabled": True, "floor": 0.5, "ceiling": 1.0, "min_mult": 0.0,
    }
    ctx = _ctx([blk], ["BLK"], cfg)
    SizeAndEmitTask().run(ctx)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"
    assert "one_share_floor_roundups" not in ctx.counters
    assert ctx.orders == []  # not bought under any reason


def test_asymptotically_tiny_sigma_multiplier_is_still_eligible_for_rescue():
    """Unlike conviction_multiplier (whose `min_mult` floor is reachable
    EXACTLY, tested above), sigma_multiplier's `m = med/sigma` ratio is
    strictly positive for any positive (sigma, sigma_median) pair -- even
    with floor=0.0 configured, `max(0.0, m)` never equals 0.0, only
    approaches it asymptotically (verified: sigma_multiplier(1e5, 0.1,
    floor=0.0) == 1e-06, not 0.0). A max_pct that is tiny-but-genuinely-
    positive is NOT the same as the exactly-zero case above -- it is a
    legitimate (if extreme) model-derived target that legitimately rounds
    to 0 whole shares at a high enough price, so it correctly REMAINS
    eligible for the one-share floor rescue when flag is ON."""
    from renquant_pipeline.kernel.sizing import sigma_multiplier
    assert sigma_multiplier(1e5, 0.1, {"enabled": True, "floor": 0.0,
                                        "ceiling": 1.0}) > 0.0

    blk = _cand("BLK", panel_score=0.001, sigma=1e5)
    anchor = _cand("OXY", panel_score=0.001, sigma=0.1)  # sets a low sigma_median
    cfg = _flag_on_config()
    cfg["ranking"]["panel_scoring"]["sigma_sizing"] = {
        "enabled": True, "floor": 0.0, "ceiling": 1.0,
    }
    ctx = _ctx([anchor, blk], ["BLK"], cfg, prices={"BLK": BLK_PRICE})
    SizeAndEmitTask().run(ctx)
    assert len(ctx.orders) == 1
    assert ctx.orders[0]["ticker"] == "BLK"
    assert ctx.orders[0]["shares"] == 1
    assert ctx.orders[0]["size_floor_reason"] == "one_share_floor_round_up"


def test_negative_max_pct_is_unreachable_by_construction():
    """(no negative-target case exists): document + pin the invariant
    directly against the actual multiplier functions, rather than only
    asserting it indirectly through SizeAndEmitTask."""
    from renquant_pipeline.kernel.kelly import kelly_target_pct
    from renquant_pipeline.kernel.sizing import (
        conviction_multiplier, sigma_multiplier,
    )
    # kelly_target_pct: max(0.0, ...) floors every input combination,
    # including a deliberately negative mu (which should mean "no edge",
    # not "short" -- this function has no short-selling concept).
    assert kelly_target_pct(mu=-5.0, sigma=0.2, max_pct=0.15) == 0.0
    assert kelly_target_pct(mu=5.0, sigma=0.2, max_pct=-0.15) >= 0.0
    # conviction_multiplier / sigma_multiplier: clipped into
    # [min_mult, 1.0] / [floor, ceiling] for any panel_score/sigma input,
    # including out-of-range and negative scores.
    assert conviction_multiplier(-999.0, {"enabled": True, "floor": 0.0,
                                           "ceiling": 1.0, "min_mult": 0.0}) >= 0.0
    assert sigma_multiplier(-1.0, 0.2, {"enabled": True, "floor": 0.0,
                                         "ceiling": 1.0}) >= 0.0


def test_off_vs_on_sweep_only_rounds_to_zero_names_differ():
    """Frozen OFF-vs-ON shadow protocol (codex review): sweep a
    representative panel of candidates spanning every regime from the
    intended-notional contract above, run the SAME panel through
    SizeAndEmitTask with the flag OFF and then ON, and assert the ONLY
    behavioural delta between the two runs is exactly the RESCUED-shaped
    candidate (BLK) -- every NORMAL and ZERO-target candidate must size
    IDENTICALLY (same shares, same invest, same block reason) in both
    runs. This is the surgical-scope proof: the change touches exactly
    the rounds-to-zero-due-to-price case and nothing else."""
    def _panel():
        return [
            _cand("OXY", panel_score=0.6),                    # (a) normal, cheap
            _cand("BLK", panel_score=0.001),                  # (b) rescued candidate
            _cand("BKNG", panel_score=0.6),                   # (a) normal, but 1 share > regime cap
            _cand("NEG", panel_score=-0.11),                  # admission-gate blocked
        ]
    prices = {"OXY": 48.0, "BLK": BLK_PRICE, "BKNG": 5_000.0, "NEG": 30.0}
    tickers = ["OXY", "BLK", "BKNG", "NEG"]

    def _run(flag_on):
        cfg = _flag_on_config() if flag_on else _config()
        ctx = _ctx(_panel(), tickers, cfg, prices=prices)
        SizeAndEmitTask().run(ctx)
        by_ticker = {o["ticker"]: (o["shares"], o["invest"]) for o in ctx.orders}
        blocked = dict(getattr(ctx, "_blocked_by_ticker", {}) or {})
        return by_ticker, blocked

    off_orders, off_blocked = _run(flag_on=False)
    on_orders, on_blocked = _run(flag_on=True)

    # BLK is the only name whose fate changes: dropped OFF, bought-at-1-share ON.
    assert "BLK" not in off_orders
    assert off_blocked.get("BLK") == "size_insufficient_cash"
    assert on_orders.get("BLK") == (1, BLK_PRICE)
    assert "BLK" not in on_blocked

    # Every OTHER name sizes/blocks identically in both runs.
    for ticker in ("OXY", "BKNG", "NEG"):
        assert off_orders.get(ticker) == on_orders.get(ticker), ticker
        assert off_blocked.get(ticker) == on_blocked.get(ticker), ticker
