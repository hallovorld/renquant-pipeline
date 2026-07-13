"""Tests for crypto portfolio construction (G2 v3)."""
from __future__ import annotations

from datetime import date

import pytest

from renquant_pipeline.kernel.crypto_portfolio import (
    ActionType,
    CryptoPortfolioConfig,
    Position,
    PortfolioAction,
    SleeveState,
    check_stop,
    compute_portfolio_actions,
    compute_target_weights,
    is_in_cooldown,
)


class TestComputeTargetWeights:
    def test_equal_weight_two_pairs(self) -> None:
        w = compute_target_weights(["BTC-USD", "ETH-USD"], max_position_pct=1.0)
        assert len(w) == 2
        assert abs(w["BTC-USD"] - 0.5) < 1e-6
        assert abs(w["ETH-USD"] - 0.5) < 1e-6

    def test_equal_weight_five_pairs(self) -> None:
        pairs = [f"PAIR{i}-USD" for i in range(5)]
        w = compute_target_weights(pairs)
        for p in pairs:
            assert abs(w[p] - 0.2) < 1e-6

    def test_cap_applied(self) -> None:
        w = compute_target_weights(["BTC-USD"], max_position_pct=0.40)
        assert abs(w["BTC-USD"] - 0.40) < 1e-6

    def test_empty_returns_empty(self) -> None:
        assert compute_target_weights([]) == {}


class TestCheckStop:
    def test_stop_hit(self) -> None:
        pos = Position("BTC-USD", 0.01, 100_000.0, date(2025, 1, 1), current_price=90_000.0)
        assert check_stop(pos, 0.12) is False
        pos.current_price = 88_001.0
        assert check_stop(pos, 0.12) is False
        pos.current_price = 88_000.0
        assert check_stop(pos, 0.12) is True

    def test_exactly_at_stop(self) -> None:
        pos = Position("BTC-USD", 0.01, 100_000.0, date(2025, 1, 1), current_price=88_000.0)
        assert check_stop(pos, 0.12) is True

    def test_zero_entry_price(self) -> None:
        pos = Position("BTC-USD", 0.01, 0.0, date(2025, 1, 1), current_price=100.0)
        assert check_stop(pos, 0.12) is False


class TestIsCooldown:
    def test_in_cooldown(self) -> None:
        stops = {"BTC-USD": date(2025, 7, 1)}
        assert is_in_cooldown("BTC-USD", stops, date(2025, 7, 10), 14) is True

    def test_cooldown_expired(self) -> None:
        stops = {"BTC-USD": date(2025, 7, 1)}
        assert is_in_cooldown("BTC-USD", stops, date(2025, 7, 15), 14) is False

    def test_no_stop_history(self) -> None:
        assert is_in_cooldown("BTC-USD", {}, date(2025, 7, 10), 14) is False


class TestComputePortfolioActions:
    def _cfg(self, **kw) -> CryptoPortfolioConfig:
        defaults = dict(sleeve_budget_usd=5000.0, min_order_usd=10.0)
        defaults.update(kw)
        return CryptoPortfolioConfig(**defaults)

    def test_new_long_signal_creates_buy(self) -> None:
        signals = {"BTC-USD": 1, "ETH-USD": 1, "SOL-USD": 1}
        prices = {"BTC-USD": 100_000.0, "ETH-USD": 3_000.0, "SOL-USD": 200.0}
        state = SleeveState()
        actions = compute_portfolio_actions(signals, prices, state, self._cfg())

        buys = [a for a in actions if a.action == ActionType.BUY]
        assert len(buys) == 3
        for b in buys:
            assert b.target_notional == pytest.approx(5000.0 / 3, rel=0.01)

    def test_signal_flip_to_cash_creates_sell(self) -> None:
        state = SleeveState(positions={
            "BTC-USD": Position("BTC-USD", 0.025, 100_000.0, date(2025, 1, 1), current_price=100_000.0),
        })
        signals = {"BTC-USD": 0}
        prices = {"BTC-USD": 100_000.0}
        actions = compute_portfolio_actions(signals, prices, state, self._cfg())

        sells = [a for a in actions if a.action == ActionType.SELL]
        assert len(sells) == 1
        assert sells[0].pair == "BTC-USD"

    def test_stop_hit_creates_sell_and_cooldown(self) -> None:
        state = SleeveState(positions={
            "BTC-USD": Position("BTC-USD", 0.05, 100_000.0, date(2025, 1, 1), current_price=87_000.0),
        })
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 87_000.0}
        today = date(2025, 7, 1)
        actions = compute_portfolio_actions(signals, prices, state, self._cfg(), today=today)

        sells = [a for a in actions if a.action == ActionType.SELL and "stop" in a.reason.lower()]
        assert len(sells) == 1
        assert state.stopped_pairs.get("BTC-USD") == today

    def test_cooldown_blocks_reentry(self) -> None:
        state = SleeveState(stopped_pairs={"BTC-USD": date(2025, 7, 1)})
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 100_000.0}
        actions = compute_portfolio_actions(
            signals, prices, state, self._cfg(stop_cooldown_days=14),
            today=date(2025, 7, 10),
        )

        buys = [a for a in actions if a.action == ActionType.BUY]
        assert len(buys) == 0

    def test_cooldown_expired_allows_entry(self) -> None:
        state = SleeveState(stopped_pairs={"BTC-USD": date(2025, 7, 1)})
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 100_000.0}
        actions = compute_portfolio_actions(
            signals, prices, state, self._cfg(stop_cooldown_days=14),
            today=date(2025, 7, 16),
        )

        buys = [a for a in actions if a.action == ActionType.BUY]
        assert len(buys) == 1

    def test_drawdown_halt_blocks_entries(self) -> None:
        state = SleeveState(high_water_mark=6000.0)
        cfg = self._cfg(sleeve_budget_usd=5000.0, max_drawdown_pct=0.15)
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 100_000.0}
        actions = compute_portfolio_actions(signals, prices, state, cfg)

        buys = [a for a in actions if a.action == ActionType.BUY]
        assert len(buys) == 0
        assert state.halted is True

    def test_drift_triggers_resize(self) -> None:
        state = SleeveState(positions={
            "BTC-USD": Position("BTC-USD", 0.05, 50_000.0, date(2025, 1, 1), current_price=60_000.0),
        })
        cfg = self._cfg(sleeve_budget_usd=5000.0, drift_rebalance_pct=0.15)
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 60_000.0}
        actions = compute_portfolio_actions(signals, prices, state, cfg)

        resizes = [a for a in actions if a.action == ActionType.RESIZE]
        assert len(resizes) == 1

    def test_small_drift_no_resize(self) -> None:
        state = SleeveState(positions={
            "BTC-USD": Position("BTC-USD", 0.019, 100_000.0, date(2025, 1, 1), current_price=100_000.0),
        })
        cfg = self._cfg(sleeve_budget_usd=5000.0, drift_rebalance_pct=0.15, max_position_pct=0.40)
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 100_000.0}
        actions = compute_portfolio_actions(signals, prices, state, cfg)

        resizes = [a for a in actions if a.action == ActionType.RESIZE]
        assert len(resizes) == 0

    def test_pair_dropped_from_universe_sells(self) -> None:
        state = SleeveState(positions={
            "BTC-USD": Position("BTC-USD", 0.025, 100_000.0, date(2025, 1, 1), current_price=100_000.0),
        })
        signals = {"ETH-USD": 1}
        prices = {"BTC-USD": 100_000.0, "ETH-USD": 3_000.0}
        actions = compute_portfolio_actions(signals, prices, state, self._cfg())

        sells = [a for a in actions if a.action == ActionType.SELL and a.pair == "BTC-USD"]
        assert len(sells) == 1
        assert "dropped" in sells[0].reason

    def test_all_cash_no_actions(self) -> None:
        signals = {"BTC-USD": 0, "ETH-USD": 0}
        prices = {"BTC-USD": 100_000.0, "ETH-USD": 3_000.0}
        state = SleeveState()
        actions = compute_portfolio_actions(signals, prices, state, self._cfg())

        assert len(actions) == 0

    def test_position_cap(self) -> None:
        signals = {"BTC-USD": 1}
        prices = {"BTC-USD": 100_000.0}
        state = SleeveState()
        cfg = self._cfg(sleeve_budget_usd=5000.0, max_position_pct=0.40)
        actions = compute_portfolio_actions(signals, prices, state, cfg)

        buys = [a for a in actions if a.action == ActionType.BUY]
        assert len(buys) == 1
        assert buys[0].target_notional == pytest.approx(2000.0)
