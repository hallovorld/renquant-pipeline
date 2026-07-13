"""Crypto portfolio construction — equal-weight, risk-gated (G2 v3).

Translates trend signals into portfolio actions: target weights, order intents,
drawdown circuit breaker, per-pair stop management, and drift rebalancing.

Uses the existing update_drawdown_circuit_breaker from portfolio.py (R1).
No equity coupling — operates on the crypto sleeve independently.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .portfolio import update_drawdown_circuit_breaker


class ActionType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    RESIZE = "RESIZE"
    HOLD = "HOLD"


@dataclass(frozen=True)
class CryptoPortfolioConfig:
    sleeve_budget_usd: float = 5350.0
    max_drawdown_pct: float = 0.15
    max_position_pct: float = 0.40
    min_order_usd: float = 10.0
    drift_rebalance_pct: float = 0.15
    stop_pct: float = 0.12
    stop_cooldown_days: int = 14


@dataclass
class Position:
    pair: str
    qty: float
    entry_price: float
    entry_date: date
    current_price: float = 0.0
    stop_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price


@dataclass(frozen=True)
class PortfolioAction:
    pair: str
    action: ActionType
    target_notional: float
    current_notional: float
    reason: str


@dataclass
class SleeveState:
    positions: dict[str, Position] = field(default_factory=dict)
    high_water_mark: float = 0.0
    halted: bool = False
    stopped_pairs: dict[str, date] = field(default_factory=dict)


def compute_target_weights(
    long_pairs: list[str],
    max_position_pct: float = 0.40,
) -> dict[str, float]:
    """Equal-weight among active LONG signals, capped at max_position_pct."""
    if not long_pairs:
        return {}
    raw_weight = 1.0 / len(long_pairs)
    capped = min(raw_weight, max_position_pct)
    return {pair: capped for pair in long_pairs}


def check_stop(pos: Position, stop_pct: float) -> bool:
    """Returns True if the position has hit its stop level."""
    if pos.entry_price <= 0 or pos.current_price <= 0:
        return False
    stop_price = pos.entry_price * (1.0 - stop_pct)
    return pos.current_price <= stop_price


def is_in_cooldown(
    pair: str,
    stopped_pairs: dict[str, date],
    today: date,
    cooldown_days: int,
) -> bool:
    """Check if a pair is still in stop cooldown."""
    stop_date = stopped_pairs.get(pair)
    if stop_date is None:
        return False
    return (today - stop_date).days < cooldown_days


def compute_portfolio_actions(
    signals: dict[str, int],
    prices: dict[str, float],
    state: SleeveState,
    cfg: CryptoPortfolioConfig,
    today: date | None = None,
) -> list[PortfolioAction]:
    """Given signals and current state, produce portfolio actions.

    signals: {pair: 0 or 1}
    prices: {pair: current_price}
    """
    if today is None:
        today = date.today()

    actions: list[PortfolioAction] = []

    for pair, pos in state.positions.items():
        if pair in prices:
            pos.current_price = prices[pair]

    total_mv = sum(p.market_value for p in state.positions.values())
    sleeve_value = max(total_mv, cfg.sleeve_budget_usd)

    new_hwm, should_halt = update_drawdown_circuit_breaker(
        sleeve_value, state.high_water_mark, cfg.max_drawdown_pct,
    )
    state.high_water_mark = new_hwm
    if should_halt:
        state.halted = True

    for pair, pos in list(state.positions.items()):
        if check_stop(pos, cfg.stop_pct):
            actions.append(PortfolioAction(
                pair=pair,
                action=ActionType.SELL,
                target_notional=0.0,
                current_notional=pos.market_value,
                reason=f"R2 stop hit: price {pos.current_price:.2f} <= stop {pos.entry_price * (1 - cfg.stop_pct):.2f}",
            ))
            state.stopped_pairs[pair] = today
            continue

    for pair, pos in state.positions.items():
        if pair not in signals or signals.get(pair, 0) == 0:
            if pos.market_value > 0:
                actions.append(PortfolioAction(
                    pair=pair,
                    action=ActionType.SELL,
                    target_notional=0.0,
                    current_notional=pos.market_value,
                    reason="signal flipped to CASH" if pair in signals else "pair dropped from universe",
                ))

    long_pairs = [
        p for p, s in signals.items()
        if s == 1
        and not is_in_cooldown(p, state.stopped_pairs, today, cfg.stop_cooldown_days)
        and not state.halted
    ]

    weights = compute_target_weights(long_pairs, cfg.max_position_pct)

    for pair, weight in weights.items():
        target = cfg.sleeve_budget_usd * weight
        pos = state.positions.get(pair)
        current = pos.market_value if pos else 0.0

        if current < cfg.min_order_usd and target >= cfg.min_order_usd:
            actions.append(PortfolioAction(
                pair=pair,
                action=ActionType.BUY,
                target_notional=target,
                current_notional=current,
                reason=f"new LONG signal, weight={weight:.2%}",
            ))
        elif current >= cfg.min_order_usd and target >= cfg.min_order_usd:
            drift = abs(current - target) / target if target > 0 else 0
            if drift > cfg.drift_rebalance_pct:
                actions.append(PortfolioAction(
                    pair=pair,
                    action=ActionType.RESIZE,
                    target_notional=target,
                    current_notional=current,
                    reason=f"drift {drift:.1%} > {cfg.drift_rebalance_pct:.0%} threshold",
                ))

    return actions
