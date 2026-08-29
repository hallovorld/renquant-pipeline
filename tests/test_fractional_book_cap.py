"""S-FRAC v2 STAGE 3 AC #8 — `fractional_max_book_pct` (2026-08-28).

Design: renquant-orchestrator doc/design/2026-07-02-s-frac-fractional-v2.md
§3.3/§3.4 (the unprotected-notional budget) and §6 stage 3 (the AC + flag
list). Progress: doc/progress/2026-08-28-fractional-max-book-pct.md.

Frozen contract under test:

* fractional ON and a fractional BUY intent ⇒ the post-trade fractional
  sleeve (held non-integral positions at ctx.prices + fractional intents
  already emitted this bar, in emission order) ≤ max_book_pct × PV;
* intents beyond the cap are floored (6 dp) to the remaining room; room
  below the fractional floor ⇒ dropped with skip reason
  `fractional_book_cap`;
* flag OFF ⇒ the cap code path is NEVER invoked and outputs are
  byte-identical to a config with no execution block;
* malformed / negative `max_book_pct` ⇒ fail CLOSED for fractional sizing
  only (cap = 0); whole-share sizing never consults the key.

Fixture = the stage-2 BLK case: PV $10k, cash $5k, $1,100 name, compounded
target $381 ⇒ fractional qty 0.346363 ($380.9993).
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import math
from types import SimpleNamespace

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel import sizing as sizing_mod
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask
from renquant_pipeline.kernel.pipeline.task_rotation import (
    BuildPairsTask,
    EmitRotationsTask,
    ValidatePairsTask,
)
from renquant_pipeline.kernel.sizing import (
    DEFAULT_FRACTIONAL_MAX_BOOK_PCT,
    FRACTIONAL_BOOK_CAP_DOWNSIZED,
    FRACTIONAL_BOOK_CAP_SKIP_REASON,
    apply_fractional_book_cap,
    fractional_book_exposure,
    fractional_max_book_pct,
    is_fractional_quantity,
)

PV = 10_000.0
CASH = 5_000.0
BLK_PRICE = 1_100.0
REGIME_CAP_PCT = 0.15
CONV_MIN_MULT = 0.254
BLK_FRACTIONAL_QTY = 0.346363                       # floor6dp(381/1100)
BLK_INTENT = BLK_FRACTIONAL_QTY * BLK_PRICE          # $380.9993
DUST_FLOOR = 25.0                                    # §7.3 default


def _floor6(x: float) -> float:
    return math.floor(x * 1_000_000) / 1_000_000


def _cand(ticker, panel_score=0.001, *, expected_return=0.04, mu=0.04, sigma=0.2):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=expected_return,
        expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _config(*, fractional=None):
    cfg = {
        "regime_params": {"BULL_CALM": {
            "max_position_pct": REGIME_CAP_PCT,
            "cash_reserve_pct": 0.0,
            "max_concurrent_positions": 8,
        }},
        "ranking": {"panel_scoring": {
            "enabled": True,
            "sizing": {"enabled": True, "floor": 0.5, "ceiling": 1.0,
                       "min_mult": CONV_MIN_MULT},
            "sigma_sizing": {},
        }, "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    if fractional is not None:
        cfg["execution"] = {"fractional_shares": fractional}
    return cfg


def _frac(**extra):
    return {"enabled": True, **extra}


def _run(config, *, tickers=("BLK",), prices=None, holdings=None):
    ranked = [_cand(t) for t in tickers]
    prices = prices or {t: BLK_PRICE for t in tickers}
    ctx = InferenceContext(
        config=config, today=dt.date(2026, 8, 28), regime="BULL_CALM",
        confidence=1.0, bear_only=False, portfolio_value=PV, cash=CASH,
        prices=prices, ranked=ranked, models={},
        holdings=holdings or {},
    )
    ctx._selected = list(tickers)  # noqa: SLF001
    SizeAndEmitTask().run(ctx)
    return ctx


def _snapshot(config, **kw):
    ctx = _run(config, **kw)
    return (copy.deepcopy(ctx.orders),
            dict(getattr(ctx, "_blocked_by_ticker", {})),
            dict(ctx.counters))


# ── Config reader ────────────────────────────────────────────────────────────

def test_reader_default_absent_is_ten_pct():
    assert fractional_max_book_pct(None) == DEFAULT_FRACTIONAL_MAX_BOOK_PCT == 0.10
    assert fractional_max_book_pct({}) == 0.10
    assert fractional_max_book_pct({"execution": {"fractional_shares": {"enabled": True}}}) == 0.10


@pytest.mark.parametrize("raw,expected", [(0.05, 0.05), (0, 0.0), (1.0, 1.0), (1, 1.0)])
def test_reader_accepts_real_numbers_in_unit_interval(raw, expected):
    cfg = {"execution": {"fractional_shares": {"max_book_pct": raw}}}
    assert fractional_max_book_pct(cfg) == expected


@pytest.mark.parametrize("raw", ["0.1", -0.1, True, False, 1.5, None, float("nan"), float("inf"), [0.1]])
def test_reader_malformed_fails_closed_to_zero_and_logs(raw, caplog):
    cfg = {"execution": {"fractional_shares": {"max_book_pct": raw}}}
    with caplog.at_level(logging.WARNING, logger="kernel.sizing"):
        assert fractional_max_book_pct(cfg) == 0.0
    assert any("max_book_pct" in r.getMessage() and "failing CLOSED" in r.getMessage()
               for r in caplog.records)


# ── Pure cap arithmetic ──────────────────────────────────────────────────────

def test_is_fractional_quantity():
    assert is_fractional_quantity(0.5)
    assert is_fractional_quantity(2.000001)
    assert not is_fractional_quantity(2.0)
    assert not is_fractional_quantity(3)
    assert not is_fractional_quantity(0.0)
    assert not is_fractional_quantity(None)
    assert not is_fractional_quantity(True)
    assert not is_fractional_quantity("0.5x")
    assert not is_fractional_quantity(float("nan"))


def test_apply_cap_exact_boundary_is_unchanged():
    shares, outcome = apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=BLK_INTENT, exposure=0.0, floor_notional=DUST_FLOOR,
    )
    assert (shares, outcome) == (BLK_FRACTIONAL_QTY, None)
    # One cent over the room ⇒ downsized (never silently over the cap).
    shares, outcome = apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=BLK_INTENT - 0.01, exposure=0.0, floor_notional=DUST_FLOOR,
    )
    assert outcome == FRACTIONAL_BOOK_CAP_DOWNSIZED
    assert shares * BLK_PRICE <= BLK_INTENT - 0.01 + 1e-9


def test_apply_cap_downsizes_to_room_floored_never_rounded_up():
    shares, outcome = apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=800.0, floor_notional=DUST_FLOOR,
    )
    assert outcome == FRACTIONAL_BOOK_CAP_DOWNSIZED
    assert shares == _floor6(200.0 / BLK_PRICE) == 0.181818
    assert shares * BLK_PRICE <= 200.0


def test_apply_cap_drops_when_room_below_floor_named_reason():
    # Room $10: below the $25 fractional floor ⇒ drop, named reason.
    assert apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=990.0, floor_notional=DUST_FLOOR,
    ) == (0.0, FRACTIONAL_BOOK_CAP_SKIP_REASON)
    # The contract's $1 broker-minimum path (floor = $1): $10 of room is
    # usable and the intent is downsized instead of dropped.
    shares, outcome = apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=990.0, floor_notional=1.0,
    )
    assert outcome == FRACTIONAL_BOOK_CAP_DOWNSIZED and shares == _floor6(10.0 / BLK_PRICE)
    # Room $0.50 < $1 broker minimum ⇒ dropped even on the $1 floor.
    assert apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=999.5, floor_notional=1.0,
    ) == (0.0, FRACTIONAL_BOOK_CAP_SKIP_REASON)
    # Exposure already over the cap / cap zero ⇒ dropped.
    assert apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=1_200.0, floor_notional=DUST_FLOOR,
    )[1] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    assert apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=0.0, exposure=0.0, floor_notional=DUST_FLOOR,
    )[1] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    # Unknown exposure ⇒ dropped (fail closed).
    assert apply_fractional_book_cap(
        BLK_FRACTIONAL_QTY, BLK_PRICE,
        cap_notional=1_000.0, exposure=None, floor_notional=DUST_FLOOR,
    ) == (0.0, FRACTIONAL_BOOK_CAP_SKIP_REASON)


def test_exposure_counts_fractional_holdings_and_intents_not_whole_shares():
    holdings = {
        "ABC": SimpleNamespace(shares=0.5),     # fractional: 0.5 × 1100 = 550
        "AAPL": SimpleNamespace(shares=2),      # integral ⇒ not in the sleeve
        "MSFT": SimpleNamespace(shares=3.0),    # integral float ⇒ not in the sleeve
        "NOQTY": SimpleNamespace(),             # no recorded quantity ⇒ ignored
    }
    prices = {"ABC": 1_100.0, "AAPL": 200.0, "MSFT": 400.0}
    orders = [
        {"ticker": "X", "shares": 0.25, "price": 100.0, "invest": 25.0,
         "sizing_mode": "fractional"},
        {"ticker": "Y", "shares": 0.1, "price": 100.0, "invest": 10.0},   # untagged but non-integral
        {"ticker": "Z", "shares": 3, "price": 100.0, "invest": 300.0},     # whole-share ⇒ ignored
        "not-a-dict",
    ]
    assert fractional_book_exposure(holdings, prices, orders) == pytest.approx(550.0 + 25.0 + 10.0)
    assert fractional_book_exposure({}, {}, []) == 0.0
    assert fractional_book_exposure(None, None, None) == 0.0
    # A fractional holding with no usable mark ⇒ UNKNOWN (None), never 0.
    assert fractional_book_exposure({"ABC": SimpleNamespace(shares=0.5)}, {}, []) is None
    assert fractional_book_exposure({"ABC": SimpleNamespace(shares=0.5)}, {"ABC": 0.0}, []) is None
    assert fractional_book_exposure({"ABC": SimpleNamespace(shares=0.5)}, {"ABC": float("nan")}, []) is None


# ── Flag OFF: inert, and the cap is never invoked ───────────────────────────

def test_flag_off_cap_path_never_invoked_and_byte_inert(monkeypatch):
    baseline_orders, baseline_blocked, baseline_counters = _snapshot(
        _config(), tickers=("OXY", "BLK"), prices={"OXY": 48.0, "BLK": BLK_PRICE},
    )

    def _boom(*_a, **_k):
        raise AssertionError("fractional book cap invoked with the flag OFF")

    monkeypatch.setattr(sizing_mod, "cap_fractional_intent_to_book", _boom)
    for frac in (
        None,
        {"enabled": False},
        {"enabled": False, "max_book_pct": 0.01},
        {"enabled": False, "max_book_pct": -1},          # malformed but flag off
        {"enabled": "true", "max_book_pct": 0.0},       # non-bool enabled ⇒ OFF
        {"max_book_pct": 0.5},
    ):
        orders, blocked, counters = _snapshot(
            _config(fractional=frac), tickers=("OXY", "BLK"),
            prices={"OXY": 48.0, "BLK": BLK_PRICE},
        )
        assert orders == baseline_orders
        assert blocked == baseline_blocked
        assert counters == baseline_counters
    assert len(baseline_orders) == 1 and baseline_orders[0]["ticker"] == "OXY"
    assert isinstance(baseline_orders[0]["shares"], int)
    assert "size_cap_reason" not in baseline_orders[0]
    assert baseline_blocked["BLK"] == "size_insufficient_cash"
    assert not any("book_cap" in k for k in baseline_counters)


# ── Flag ON: boundary / downsizing / drop / malformed / existing exposure ────

def test_task_cap_exactly_at_boundary_leaves_intent_unchanged():
    ctx = _run(_config(fractional=_frac(max_book_pct=BLK_INTENT / PV)))
    assert [o["ticker"] for o in ctx.orders] == ["BLK"]
    o = ctx.orders[0]
    assert o["shares"] == BLK_FRACTIONAL_QTY
    assert o["sizing_mode"] == "fractional"
    assert "size_cap_reason" not in o
    assert FRACTIONAL_BOOK_CAP_DOWNSIZED not in ctx.counters
    assert "BLK" not in getattr(ctx, "_blocked_by_ticker", {})
    # Default cap (10% = $1,000) also leaves the $381 intent untouched.
    ctx = _run(_config(fractional=_frac()))
    assert ctx.orders[0]["shares"] == BLK_FRACTIONAL_QTY
    assert "size_cap_reason" not in ctx.orders[0]


def test_task_downsizes_intent_to_remaining_room_and_stamps_ledger():
    ctx = _run(_config(fractional=_frac(max_book_pct=0.02)))    # cap $200
    assert [o["ticker"] for o in ctx.orders] == ["BLK"]
    o = ctx.orders[0]
    assert o["shares"] == _floor6(200.0 / BLK_PRICE) == 0.181818
    assert o["invest"] <= 200.0
    assert o["size_cap_reason"] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    assert o["sizing_mode"] == "fractional"
    assert ctx.counters[FRACTIONAL_BOOK_CAP_DOWNSIZED] == 1
    assert "BLK" not in getattr(ctx, "_blocked_by_ticker", {})


def test_task_drops_below_floor_with_named_skip_reason():
    # Held fractional ABC 0.9 × $1,100 = $990 of a $1,000 cap ⇒ room $10 <
    # $25 floor ⇒ BLK is dropped, reason `fractional_book_cap`.
    ctx = _run(
        _config(fractional=_frac()),
        prices={"BLK": BLK_PRICE, "ABC": BLK_PRICE},
        holdings={"ABC": SimpleNamespace(shares=0.9)},
    )
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == FRACTIONAL_BOOK_CAP_SKIP_REASON  # noqa: SLF001
    assert ctx.counters[f"selection_{FRACTIONAL_BOOK_CAP_SKIP_REASON}"] == 1
    assert FRACTIONAL_BOOK_CAP_DOWNSIZED not in ctx.counters


def test_task_malformed_cap_fails_closed_for_fractional_only(caplog):
    prices = {"OXY": 48.0, "BLK": BLK_PRICE}
    for raw in ("0.1", -0.1, True, 2.0, None):
        with caplog.at_level(logging.WARNING, logger="kernel.sizing"):
            ctx = _run(
                _config(fractional=_frac(max_book_pct=raw,
                                         non_fractionable_tickers=["OXY"])),
                tickers=("OXY", "BLK"), prices=prices,
            )
        # Fractional BLK: dropped with the named reason.
        assert ctx._blocked_by_ticker["BLK"] == FRACTIONAL_BOOK_CAP_SKIP_REASON  # noqa: SLF001
        # Whole-share OXY (non-fractionable ⇒ whole-share path): sized as
        # before, int quantity, no cap stamp — the key is never consulted.
        assert [o["ticker"] for o in ctx.orders] == ["OXY"]
        assert isinstance(ctx.orders[0]["shares"], int) and ctx.orders[0]["shares"] == 7
        assert "size_cap_reason" not in ctx.orders[0]
        assert any("max_book_pct" in r.getMessage() for r in caplog.records)
        caplog.clear()


def test_task_existing_fractional_exposure_counted_integral_ignored():
    # ABC 0.5 × $1,100 = $550 fractional; AAPL 2 × $200 integral (ignored).
    holdings = {"ABC": SimpleNamespace(shares=0.5), "AAPL": SimpleNamespace(shares=2)}
    prices = {"BLK": BLK_PRICE, "ABC": BLK_PRICE, "AAPL": 200.0}
    # Cap 10% = $1,000 ⇒ room $450 ≥ $381 ⇒ unchanged.
    ctx = _run(_config(fractional=_frac()), prices=prices, holdings=holdings)
    assert ctx.orders[0]["shares"] == BLK_FRACTIONAL_QTY
    assert "size_cap_reason" not in ctx.orders[0]
    # Cap 8% = $800 ⇒ room $250 ⇒ downsized to floor6(250/1100).
    ctx = _run(_config(fractional=_frac(max_book_pct=0.08)), prices=prices, holdings=holdings)
    assert ctx.orders[0]["shares"] == _floor6(250.0 / BLK_PRICE) == 0.227272
    assert ctx.orders[0]["size_cap_reason"] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    # If AAPL were fractional too (2.5 × $200 = $500): exposure $1,050 >
    # $1,000 cap ⇒ BLK dropped.
    holdings["AAPL"] = SimpleNamespace(shares=2.5)
    ctx = _run(_config(fractional=_frac()), prices=prices, holdings=holdings)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == FRACTIONAL_BOOK_CAP_SKIP_REASON  # noqa: SLF001


def test_task_unknown_mark_on_fractional_holding_fails_closed():
    ctx = _run(
        _config(fractional=_frac()),
        prices={"BLK": BLK_PRICE},                  # no price for held ABC
        holdings={"ABC": SimpleNamespace(shares=0.5)},
    )
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == FRACTIONAL_BOOK_CAP_SKIP_REASON  # noqa: SLF001


def test_task_sequential_intents_consume_room_in_emission_order():
    # Two $381 fractional intents against a 5% ($500) cap: the first (higher
    # rank) is untouched; the second gets the $119 remainder, floored.
    ctx = _run(
        _config(fractional=_frac(max_book_pct=0.05)),
        tickers=("BLK", "BLK2"), prices={"BLK": BLK_PRICE, "BLK2": BLK_PRICE},
    )
    assert [o["ticker"] for o in ctx.orders] == ["BLK", "BLK2"]
    first, second = ctx.orders
    assert first["shares"] == BLK_FRACTIONAL_QTY and "size_cap_reason" not in first
    room = 500.0 - first["invest"]
    assert second["shares"] == _floor6(room / BLK_PRICE)
    assert second["size_cap_reason"] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    assert first["invest"] + second["invest"] <= 500.0 + 1e-9
    assert ctx.counters[FRACTIONAL_BOOK_CAP_DOWNSIZED] == 1
    # A third would find < $25 of room ⇒ dropped.
    ctx = _run(
        _config(fractional=_frac(max_book_pct=0.05)),
        tickers=("BLK", "BLK2", "BLK3"),
        prices={"BLK": BLK_PRICE, "BLK2": BLK_PRICE, "BLK3": BLK_PRICE},
    )
    assert [o["ticker"] for o in ctx.orders] == ["BLK", "BLK2"]
    assert ctx._blocked_by_ticker["BLK3"] == FRACTIONAL_BOOK_CAP_SKIP_REASON  # noqa: SLF001


# ── Rotation buy-leg honours the same cap ───────────────────────────────────

def _rot_cand(ticker, *, rank_score, expected_return):
    return CandidateResult(
        ticker=ticker, raw_score=rank_score, rank_score=rank_score,
        rs_score=0.0, detail="", expected_return=expected_return,
        expected_return_horizon_days=20, panel_score=rank_score,
        mu=expected_return, mu_horizon_days=20, sigma=0.2,
    )


def _rot_cfg(fractional):
    return {
        "rotation": {
            "enabled": True, "min_expected_advantage_pct": 0.06,
            "target_horizon_days": 20, "transaction_cost_pct": 0.0,
            "min_rotation_hold_days": 30, "lt_protection_days": 30,
            "max_rotations_per_bar": 1,
        },
        "ranking": {
            "panel_scoring": {"enabled": True, "sizing": {}, "sigma_sizing": {}},
            "kelly_sizing": {"enabled": False},
        },
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.15,
                                        "cash_reserve_pct": 0.0}},
        "regime": {},
        "tax": {"short_term_rate": 0.50, "long_term_rate": 0.32,
                "long_term_threshold_days": 365},
        "wash_sale_days": 0, "max_positions_per_sector": 0, "sector_map": {},
        "execution": {"fractional_shares": fractional},
    }


def _rot_ctx(fractional, *, prior_orders=None):
    ranked = [_rot_cand("GOOG", rank_score=2.052, expected_return=+0.0252)]
    prices = {"GOOG": 1_100.0, "CRWD": 450.0, "ABC": 1_100.0}
    holdings = {"CRWD": SimpleNamespace(
        shares=2.0, rank_score=1.0, expected_return=-0.1417,
        entry_price=500.0, entry_date=dt.date(2026, 5, 1),
    )}
    ctx = InferenceContext(
        config=_rot_cfg(fractional), today=dt.date(2026, 8, 28),
        regime="BULL_CALM", confidence=1.0, portfolio_value=PV, cash=CASH,
        holdings=holdings, prices=prices, ranked=ranked,
    )
    ctx.orders.extend(prior_orders or [])
    BuildPairsTask().run(ctx)
    ValidatePairsTask().run(ctx)
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] == [("CRWD", "GOOG")]
    EmitRotationsTask().run(ctx)
    return ctx


def test_rotation_buy_leg_downsized_and_dropped_by_book_cap():
    # Untouched at the default cap: 15% × $10k = $1,500 target ⇒ 1.363636
    # GOOG shares... which already exceeds the $1,000 sleeve ⇒ downsized.
    ctx = _rot_ctx(_frac())
    assert [o["ticker"] for o in ctx.orders] == ["GOOG"]
    assert ctx.orders[0]["shares"] == _floor6(1_000.0 / 1_100.0)
    assert ctx.orders[0]["size_cap_reason"] == FRACTIONAL_BOOK_CAP_SKIP_REASON
    assert ctx.counters[FRACTIONAL_BOOK_CAP_DOWNSIZED] == 1
    # Raise the cap to 20% ⇒ $2,000 room ⇒ the $1,500 target is untouched.
    ctx = _rot_ctx(_frac(max_book_pct=0.20))
    assert ctx.orders[0]["shares"] == _floor6(1_500.0 / 1_100.0)
    assert "size_cap_reason" not in ctx.orders[0]
    # A fractional intent already emitted this bar ($990 of the $1,000
    # sleeve) fills the cap ⇒ the ENTIRE pair is skipped with the named
    # reason; no orphan exit is committed.
    prior = {"ticker": "ABC", "shares": 0.9, "price": 1_100.0, "invest": 990.0,
             "sizing_mode": "fractional", "order_type": "NEW_BUY"}
    ctx = _rot_ctx(_frac(), prior_orders=[prior])
    assert ctx.orders == [prior] and ctx.exits == []
    assert {"sell": "CRWD", "buy": "GOOG", "reason": FRACTIONAL_BOOK_CAP_SKIP_REASON} \
        in ctx.rotations_blocked


def test_rotation_flag_off_never_invokes_cap(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("fractional book cap invoked with the flag OFF")
    monkeypatch.setattr(sizing_mod, "cap_fractional_intent_to_book", _boom)
    ctx = _rot_ctx({"enabled": False, "max_book_pct": 0.0})
    assert [o["ticker"] for o in ctx.orders] == ["GOOG"]
    assert isinstance(ctx.orders[0]["shares"], int)
    assert "size_cap_reason" not in ctx.orders[0]
