"""Fractional-share execution lifecycle + backend capability negotiation.

Covers the Codex review #153 blocking points:

* #1 — live vs validation backends must NOT diverge into a zero-share fill.
  A fractional-capable backend MODELS the float; a whole-share-only backend
  FAILS FAST (never silently floors a sub-1-share order to zero).
* #2 — the full position lifecycle: a fractional BUY -> holding upsert ->
  partial sell -> full liquidate -> prune leaves NO residual state, and the
  P&L / cash conservation is correct.
* #4 — type/config discipline: OrderIntent rejects bool/str shares;
  fractional_sizing_cfg fails closed on a non-bool ``enabled``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.exits import ExitSignal, HoldingState
from renquant_pipeline.kernel.execution.backend import FakeBackend
from renquant_pipeline.kernel.execution.backend_lean import LeanBackend
from renquant_pipeline.kernel.execution.backend_sim import SimBackend
from renquant_pipeline.kernel.execution.types import (
    Fill,
    OrderIntent,
    OrderSide,
    resolve_fill_quantity,
)
from renquant_pipeline.kernel.pipeline.task_execution import (
    ExecuteBuysTask,
    ExecuteExitsTask,
    PrepareExecutionTask,
    PruneFullExitsTask,
    UpsertHoldingsTask,
)
from renquant_pipeline.kernel.sizing import fractional_sizing_cfg

TODAY = pd.Timestamp("2026-06-30")


def _buy(ticker: str, shares, price: float) -> OrderIntent:
    return OrderIntent(
        ticker=ticker, side=OrderSide.BUY, shares=shares,
        target_pct=(shares * price) / 100_000.0, today=TODAY,
        reason="buy", exit_type=None,
    )


def _sell(ticker: str, shares) -> OrderIntent:
    return OrderIntent(
        ticker=ticker, side=OrderSide.SELL, shares=shares,
        target_pct=0.0, today=TODAY, reason="exit", exit_type="model_sell",
    )


# ─────────────────────────── resolve_fill_quantity ──────────────────────────


def test_resolve_fractional_capable_preserves_float():
    out = resolve_fill_quantity(
        0.4, supports_fractional=True, backend_name="X", ticker="BLK", side="BUY"
    )
    assert out == pytest.approx(0.4)
    assert isinstance(out, float)


def test_resolve_whole_share_integral_returns_int():
    out = resolve_fill_quantity(
        17.0, supports_fractional=False, backend_name="X", ticker="AAPL", side="BUY"
    )
    assert out == 17
    assert isinstance(out, int)


def test_resolve_whole_share_fractional_fails_fast_not_zero():
    with pytest.raises(ValueError, match="fractional"):
        resolve_fill_quantity(
            0.4, supports_fractional=False, backend_name="LeanBackend",
            ticker="BLK", side="BUY",
        )


def test_resolve_rejects_nonpositive():
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            resolve_fill_quantity(
                bad, supports_fractional=True, backend_name="X",
                ticker="T", side="BUY",
            )


# ─────────────────────────── FakeBackend negotiation ────────────────────────


def test_fakebackend_default_is_whole_share_only():
    assert FakeBackend().supports_fractional is False
    assert FakeBackend(allow_fractional=True).supports_fractional is True


def test_fractional_capable_models_sub_one_share_buy():
    """A sub-1-share order yields a fractional Fill (NOT zero) and a
    fractional position — the readonly/sim path validates live behaviour."""
    be = FakeBackend(starting_cash=100_000.0, allow_fractional=True)
    be.seed_price("BLK", 1000.0, TODAY)
    fill = be.place_market_order(_buy("BLK", 0.4, 1000.0))
    assert isinstance(fill, Fill)
    assert fill.shares == pytest.approx(0.4)
    assert fill.shares > 0  # never a zero-share fill
    assert be.get_position_quantity("BLK") == pytest.approx(0.4)
    # Cash debited the true fractional notional + fees (not zero, not 1 share).
    assert be.get_cash() == pytest.approx(100_000.0 - 0.4 * 1000.0 - fill.fees)


def test_whole_share_backend_fails_fast_no_silent_zero():
    """A whole-share backend asked to fill a sub-1-share order FAILS FAST
    instead of emitting a zero-share fill or truncating to 1."""
    be = FakeBackend(starting_cash=100_000.0)  # allow_fractional defaults False
    be.seed_price("BLK", 1000.0, TODAY)
    with pytest.raises(ValueError, match="fractional"):
        be.place_market_order(_buy("BLK", 0.4, 1000.0))
    # No state mutated: no fill, no position, cash intact.
    assert be.fills == ()
    assert be.get_position_quantity("BLK") == 0.0
    assert be.get_cash() == pytest.approx(100_000.0)


def test_whole_share_backend_unchanged_for_integer_order():
    """Whole-share path stays byte-compatible: integer buy -> int Fill."""
    be = FakeBackend(starting_cash=100_000.0)
    be.seed_price("AAPL", 200.0, TODAY)
    fill = be.place_market_order(_buy("AAPL", 5, 200.0))
    assert fill.shares == 5
    assert isinstance(fill.shares, int)
    assert be.get_position_quantity("AAPL") == 5.0


# ─────────────────────── SimBackend fractional lifecycle ────────────────────


def test_sim_backend_fractional_buy_partial_then_full_liquidate():
    be = SimBackend(starting_cash=100_000.0, allow_fractional=True)
    be.update_bar_prices({"GS": 1000.0}, TODAY)
    be.place_market_order(_buy("GS", 0.8, 1000.0))
    assert be.get_position_quantity("GS") == pytest.approx(0.8)

    # Partial fractional sell — must not floor to 0.
    be.place_market_order(_sell("GS", 0.3))
    assert be.get_position_quantity("GS") == pytest.approx(0.5)

    # Full liquidate (shares=None) — sells the ENTIRE remaining fraction,
    # leaving exactly zero (no stranded residual).
    fill = be.place_market_order(
        OrderIntent(ticker="GS", side=OrderSide.SELL, shares=None,
                    target_pct=0.0, today=TODAY, reason="full",
                    exit_type="model_sell")
    )
    assert fill.shares == pytest.approx(0.5)
    assert be.get_position_quantity("GS") == 0.0


def test_sim_backend_whole_share_rejects_fractional_intent():
    be = SimBackend(starting_cash=100_000.0)  # whole-share only
    be.update_bar_prices({"GS": 1000.0}, TODAY)
    with pytest.raises(ValueError, match="fractional"):
        be.place_market_order(_buy("GS", 0.8, 1000.0))


# ─────────────────────────── LeanBackend fail-fast ──────────────────────────


class _FakeAlgoSecurity:
    def __init__(self, price):
        self.Price = price


class _FakeAlgo:
    """Minimal QCAlgorithm stub for LeanBackend negotiation tests."""

    def __init__(self):
        self.symbols = {"BLK": "BLK-SYM"}
        self.Securities = {"BLK-SYM": _FakeAlgoSecurity(1000.0)}
        self.orders = []

    def MarketOrder(self, sym, qty):  # noqa: N802 (QC API name)
        self.orders.append((sym, qty))
        return SimpleNamespace(Status="Filled", QuantityFilled=qty,
                               AverageFillPrice=1000.0)


def test_lean_backend_is_whole_share_only_and_fails_fast():
    algo = _FakeAlgo()
    be = LeanBackend(algo)
    assert be.supports_fractional is False
    with pytest.raises(ValueError, match="fractional"):
        be.place_market_order(_buy("BLK", 0.4, 1000.0))
    # Fail fast BEFORE any broker order is submitted.
    assert algo.orders == []


# ───────────────────── pipeline-level negotiation guard ─────────────────────


def _exec_ctx(backend, *, fractional_enabled):
    return SimpleNamespace(
        execution_backend=backend,
        config={"execution": {"fractional_shares": {"enabled": fractional_enabled}}},
        today=TODAY,
        fills=[],
        orders=[],
        exits=[],
        holdings={},
        last_sell_dates={},
        last_stop_exit_dates={},
    )


def test_prepare_task_fails_fast_when_fractional_meets_whole_share_backend():
    ctx = _exec_ctx(FakeBackend(), fractional_enabled=True)
    with pytest.raises(ValueError, match="fractional"):
        PrepareExecutionTask().run(ctx)


def test_prepare_task_ok_when_fractional_meets_capable_backend():
    ctx = _exec_ctx(FakeBackend(allow_fractional=True), fractional_enabled=True)
    assert PrepareExecutionTask().run(ctx) is True


def test_prepare_task_ok_when_fractional_disabled_whole_share_backend():
    ctx = _exec_ctx(FakeBackend(), fractional_enabled=False)
    assert PrepareExecutionTask().run(ctx) is True


# ──────────── end-to-end fractional lifecycle through the Tasks ─────────────


def test_fractional_buy_upsert_partial_full_sell_persists_and_pnl():
    """fractional buy -> holding upsert -> partial sell -> full liquidate ->
    prune. Asserts no residual position/holding and exact cash conservation."""
    be = FakeBackend(starting_cash=100_000.0, allow_fractional=True)
    be.seed_price("BLK", 1000.0, TODAY)
    start_cash = be.get_cash()
    ctx = _exec_ctx(be, fractional_enabled=True)

    # --- BUY a sub-1-share fractional order through the buy tasks ---
    ctx.orders = [{
        "ticker": "BLK", "shares": 0.4, "price": 1000.0,
        "target_pct": 0.004, "detail": "buy",
        "rank_score": 0.7, "panel_score": 0.5,
        "kelly_target_pct": 0.004, "regime": "BULL_CALM",
    }]
    PrepareExecutionTask().run(ctx)
    ExecuteBuysTask().run(ctx)
    UpsertHoldingsTask().run(ctx)

    assert be.get_position_quantity("BLK") == pytest.approx(0.4)
    assert "BLK" in ctx.holdings
    hs = ctx.holdings["BLK"]
    assert isinstance(hs, HoldingState)
    assert hs.entry_price == pytest.approx(1000.0)
    buy_fill = ctx.fills[0]
    assert buy_fill.side == OrderSide.BUY and buy_fill.shares == pytest.approx(0.4)

    # --- PARTIAL fractional sell (kelly trim of 0.1 share) ---
    ctx.fills = []
    ctx.exits = [("BLK", ExitSignal(should_exit=True, reason="trim",
                                    exit_type="kelly_trim", quantity=0.1))]
    ExecuteExitsTask().run(ctx)
    PruneFullExitsTask().run(ctx)
    assert be.get_position_quantity("BLK") == pytest.approx(0.3)
    assert "BLK" in ctx.holdings  # still open after a partial trim
    assert ctx.fills[0].shares == pytest.approx(0.1)

    # --- FULL liquidate (quantity=None) then prune ---
    ctx.fills = []
    ctx.exits = [("BLK", ExitSignal(should_exit=True, reason="exit",
                                    exit_type="model_sell", quantity=None))]
    ExecuteExitsTask().run(ctx)
    PruneFullExitsTask().run(ctx)
    assert be.get_position_quantity("BLK") == 0.0   # no residual
    assert "BLK" not in ctx.holdings                # holding reaped cleanly
    assert ctx.fills[0].shares == pytest.approx(0.3)

    # --- P&L / cash conservation: net shares == 0, so the round trip costs
    # exactly the sum of fees (price was flat at $1000 throughout). ---
    total_fees = sum(f.fees for f in be.fills)
    assert be.get_cash() == pytest.approx(start_cash - total_fees)
    # Every fill carried a strictly-positive share count — never a zero fill.
    assert all(f.shares > 0 for f in be.fills)


# ───────────────────────── type / config discipline (#4) ───────────────────


def test_order_intent_rejects_bool_shares():
    with pytest.raises(ValueError, match="real number"):
        _buy("BLK", True, 1000.0)


def test_order_intent_rejects_string_shares():
    with pytest.raises(ValueError, match="real number"):
        OrderIntent(ticker="BLK", side=OrderSide.BUY, shares="1.5",
                    target_pct=0.01, today=TODAY, reason="buy", exit_type=None)


def test_partial_sell_intent_rejects_bool_shares():
    with pytest.raises(ValueError, match="real number"):
        _sell("BLK", True)


def test_fractional_cfg_fails_closed_on_non_bool_enabled():
    # YAML string "false" is truthy under bool() — must fail CLOSED.
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": "false"}}}
    ) == (False, 1.0)
    # Even a string "true" stays disabled (only a genuine bool enables).
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": "true"}}}
    ) == (False, 1.0)


def test_fractional_cfg_enables_only_on_real_bool():
    assert fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": True, "min_notional": 2.5}}}
    ) == (True, 2.5)


def test_fractional_cfg_rejects_bool_min_notional():
    enabled, min_notional = fractional_sizing_cfg(
        {"execution": {"fractional_shares": {"enabled": True, "min_notional": True}}}
    )
    assert enabled is True
    assert min_notional == 1.0
