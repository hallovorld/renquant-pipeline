"""Broker reconciliation state machine — broker vs state positions (§III.4).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.4
(disaster guards / state) + §III.5. Graduates
scripts/engineering/broker_reconciliation_sm.py.

Replaces improvised mid-run warnings with one explicit policy. Invariants:
  * the BROKER is the source of truth for POSITIONS;
  * local STATE is the source of truth for INTENT/derived fields
    (sell streaks, entry anchors, wash-sale clocks).

Every position-level divergence maps to exactly one Action:
  OK            quantities agree (within tolerance)
  EXT_SELL      state held it, broker doesn't → external disposition
                (manual close / Z9 stop fill / corporate action): stamp the
                wash-sale clock, GC streaks/anchors, write a ledger row
  QUARANTINE    broker holds it, state doesn't → unknown external position:
                place NO orders on this name, alert
  ADOPT_QTY     both hold it, sizes differ (same sign) → adopt broker qty
  FORCED_COVER  sign flip (long↔short) → buy-in / external short event

Pure: no broker calls, no I/O. ``client_order_id`` gives crash-safe
idempotency — a crash between submit and persist cannot double-submit
because the broker dedups on the deterministic id.
"""
from __future__ import annotations

import hashlib
from typing import NamedTuple

OK = "OK"
EXT_SELL = "EXT_SELL"
QUARANTINE = "QUARANTINE"
ADOPT_QTY = "ADOPT_QTY"
FORCED_COVER = "FORCED_COVER"

# Quantity divergence below this (absolute OR relative) is treated as equal:
# guards against float dust and fractional-share rounding.
ABS_TOL = 0.01
REL_TOL = 1e-4


class Action(NamedTuple):
    kind: str          # OK | EXT_SELL | QUARANTINE | ADOPT_QTY | FORCED_COVER
    ticker: str
    detail: str
    state_qty: float | None = None
    broker_qty: float | None = None


def _qty_equal(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, abs(b) * REL_TOL)


def reconcile(state_positions: dict[str, float],
              broker_positions: dict[str, float]) -> list[Action]:
    """Diff state vs broker positions into an ordered list of Actions
    (one per ticker in the union, sorted for determinism)."""
    acts: list[Action] = []
    for t in sorted(set(state_positions) | set(broker_positions)):
        s = state_positions.get(t)
        b = broker_positions.get(t)
        if s is not None and b is None:
            acts.append(Action(EXT_SELL, t,
                "stamp wash-sale clock today; GC streaks/anchors; ledger row",
                state_qty=s, broker_qty=None))
        elif s is None and b is not None:
            acts.append(Action(QUARANTINE, t,
                "unknown external position: NO orders on this name; alert",
                state_qty=None, broker_qty=b))
        elif s is not None and b is not None and not _qty_equal(s, b):
            if (s > 0 > b) or (s < 0 < b):
                acts.append(Action(FORCED_COVER, t,
                    "sign flip = buy-in / external short event",
                    state_qty=s, broker_qty=b))
            else:
                acts.append(Action(ADOPT_QTY, t,
                    f"adopt broker qty {b} (was {s}); ledger row",
                    state_qty=s, broker_qty=b))
        else:
            acts.append(Action(OK, t, "", state_qty=s, broker_qty=b))
    return acts


def blocking_tickers(actions: list[Action]) -> set[str]:
    """Tickers no new order may touch this run: QUARANTINE (unknown) and
    FORCED_COVER (mid-event) names."""
    return {a.ticker for a in actions if a.kind in (QUARANTINE, FORCED_COVER)}


def client_order_id(run_id: str, ticker: str, intent: str, qty: float) -> str:
    """Deterministic, idempotent order id. A crash between submit and
    persist cannot double-submit — the broker dedups on this id. Sensitive
    to run_id (run-scoped), ticker, intent, and qty."""
    raw = f"{run_id}|{ticker}|{intent}|{qty:.4f}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]
