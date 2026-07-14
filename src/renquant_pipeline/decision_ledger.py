"""S5 decision-ledger formatter — extract per-ticker decision records from ctx.

renquant-common owns the gate-verdict ledger DB (``decision_ledger.py``, moved
from renquant-orchestrator per V-003); renquant-orchestrator still owns
``ledger_attribution.py`` for per-ticker outcomes. This module lives in the
*pipeline* because only the pipeline has access to the runtime context (``ctx``)
with candidates, exits, rotations, regime, and scores.

Two entry points:

* ``format_gate_verdicts(ctx, config, run_id, run_date)`` → list of gate-verdict
  dicts compatible with ``renquant_common.decision_ledger.write_verdicts``.
* ``format_ticker_decisions(ctx, config, run_id, run_date)`` → list of per-ticker
  decision dicts compatible with ``renquant_orchestrator.ledger_attribution.write_outcomes``
  (minus forward-return columns, which are filled later by the outcome observer).
"""
from __future__ import annotations

from typing import Any


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str | None:
    return str(v) if v is not None else None


# ---------------------------------------------------------------------------
# Gate verdicts — scope-level, one per gate per run
# ---------------------------------------------------------------------------

def format_gate_verdicts(
    ctx: Any,
    config: dict[str, Any],
    run_id: str,
    run_date: str,
) -> list[dict[str, Any]]:
    """Extract gate verdicts from the pipeline context.

    Each dict has: scope, gate, verdict ("allow" | "halve" | "block"),
    reason, inputs.  Compatible with
    ``renquant_common.decision_ledger.write_verdicts``.
    """
    scope = _run_scope(config)
    verdicts: list[dict[str, Any]] = []

    verdicts.append(_regime_verdict(ctx, scope))
    verdicts.append(_model_admission_verdict(ctx, scope))
    verdicts.append(_conviction_gate_verdict(ctx, config, scope))
    verdicts.append(_vol_gate_verdict(ctx, config, scope))
    verdicts.append(_wash_sale_verdict(ctx, scope))
    verdicts.append(_rotation_verdict(ctx, scope))

    return [v for v in verdicts if v is not None]


def _run_scope(config: dict[str, Any]) -> str:
    return config.get("strategy_id", "strategy-104")


def _regime_verdict(ctx: Any, scope: str) -> dict[str, Any]:
    regime = getattr(ctx, "regime", None) or "UNKNOWN"
    confidence = _finite(getattr(ctx, "confidence", None))
    admitted = getattr(ctx, "_regime_model_admission", None)
    ok = True
    reason = f"regime={regime}"
    if admitted is not None:
        ok = bool(getattr(admitted, "admitted", True))
        reason = getattr(admitted, "reason", reason) or reason
    return {
        "scope": scope,
        "gate": "regime",
        "verdict": "allow" if ok else "block",
        "reason": str(reason),
        "inputs": {"regime": regime, "confidence": confidence},
    }


def _model_admission_verdict(ctx: Any, scope: str) -> dict[str, Any]:
    admitted = getattr(ctx, "_regime_model_admission", None)
    if admitted is None:
        admitted = getattr(ctx, "model_admission", None)
    ok = True
    reason = "no admission gate"
    if admitted is not None:
        ok = bool(getattr(admitted, "admitted", True))
        reason = getattr(admitted, "reason", "admitted") or "admitted"
    return {
        "scope": scope,
        "gate": "model_admission",
        "verdict": "allow" if ok else "block",
        "reason": str(reason),
        "inputs": {},
    }


def _conviction_gate_verdict(
    ctx: Any, config: dict[str, Any], scope: str,
) -> dict[str, Any]:
    candidates = getattr(ctx, "candidates", None) or []
    n_candidates = len(candidates)
    n_above_floor = sum(
        1 for c in candidates
        if _finite(getattr(c, "mu", None)) is not None
        and float(getattr(c, "mu", 0)) > 0
    )
    conviction_cfg = config.get("conviction_gate", {})
    mu_floor = conviction_cfg.get("mu_floor", 0.0)

    if n_candidates == 0:
        return {
            "scope": scope, "gate": "conviction",
            "verdict": "block", "reason": "no candidates",
            "inputs": {"n_candidates": 0, "mu_floor": mu_floor},
        }
    return {
        "scope": scope, "gate": "conviction",
        "verdict": "allow" if n_above_floor > 0 else "halve",
        "reason": f"{n_above_floor}/{n_candidates} above floor",
        "inputs": {
            "n_candidates": n_candidates,
            "n_above_floor": n_above_floor,
            "mu_floor": mu_floor,
        },
    }


def _vol_gate_verdict(
    ctx: Any, config: dict[str, Any], scope: str,
) -> dict[str, Any]:
    blocked = getattr(ctx, "blocked_by", {}) or {}
    vol_blocked = [t for t, r in blocked.items() if "vol" in str(r).lower()]
    return {
        "scope": scope, "gate": "vol_gate",
        "verdict": "block" if vol_blocked else "allow",
        "reason": f"{len(vol_blocked)} blocked by vol" if vol_blocked else "none blocked",
        "inputs": {"vol_blocked_tickers": vol_blocked[:10]},
    }


def _wash_sale_verdict(ctx: Any, scope: str) -> dict[str, Any]:
    blocked = getattr(ctx, "blocked_by", {}) or {}
    ws_blocked = [t for t, r in blocked.items() if "wash" in str(r).lower()]
    return {
        "scope": scope, "gate": "wash_sale",
        "verdict": "block" if ws_blocked else "allow",
        "reason": f"{len(ws_blocked)} wash-sale blocked" if ws_blocked else "none blocked",
        "inputs": {"wash_sale_blocked_tickers": ws_blocked[:10]},
    }


def _rotation_verdict(ctx: Any, scope: str) -> dict[str, Any]:
    rotations = getattr(ctx, "rotations", None) or []
    # Same bar as format_rotation_decisions()'s "executed" field — net_advantage
    # merely being positive does not mean a rotation clears its own threshold
    # (tax drag / friction margin), and the gate-level verdict must not report
    # allow/viable for rotations that would not actually execute.
    executed = [
        r for r in rotations
        if (getattr(r, "net_advantage", 0) or 0) >= (getattr(r, "threshold", 0) or 0)
    ]
    return {
        "scope": scope, "gate": "rotation",
        "verdict": "allow" if executed else "halve",
        "reason": f"{len(executed)}/{len(rotations)} rotations viable",
        "inputs": {"n_considered": len(rotations), "n_viable": len(executed)},
    }


# ---------------------------------------------------------------------------
# Per-ticker decisions — one per ticker per run
# ---------------------------------------------------------------------------

def format_ticker_decisions(
    ctx: Any,
    config: dict[str, Any],
    run_id: str,
    run_date: str,
) -> list[dict[str, Any]]:
    """Extract per-ticker decision records from the pipeline context.

    Each dict has: as_of, scope, ticker, gate (= decision_type), verdict,
    entry_price, metadata_json fields.  Compatible with
    ``renquant_orchestrator.ledger_attribution.write_outcomes`` (minus
    forward-return columns which the outcome observer fills later).
    """
    scope = _run_scope(config)
    decisions: list[dict[str, Any]] = []

    candidates = getattr(ctx, "candidates", None) or []
    entries_raw = getattr(ctx, "entries", None) or []
    exits_raw = getattr(ctx, "exits", None) or []
    blocked = getattr(ctx, "blocked_by", {}) or {}
    holdings = getattr(ctx, "holdings", {}) or {}
    scores = getattr(ctx, "scores", {}) or {}

    entry_tickers = set()
    for entry in entries_raw:
        ticker = _entry_ticker(entry)
        if ticker:
            entry_tickers.add(ticker)

    exit_tickers: dict[str, str] = {}
    for exit_item in exits_raw:
        ticker, reason = _exit_info(exit_item)
        if ticker:
            exit_tickers[ticker] = reason

    for c in candidates:
        ticker = getattr(c, "ticker", None)
        if ticker is None:
            continue
        ticker = str(ticker)
        block_reason = blocked.get(ticker)

        if ticker in entry_tickers:
            decision_type = "buy"
            verdict = "allow"
        elif block_reason:
            decision_type = "blocked"
            verdict = "block"
        else:
            decision_type = "no_trade"
            verdict = "halve"

        decisions.append({
            "as_of": run_date,
            "scope": scope,
            "ticker": ticker,
            "gate": decision_type,
            "verdict": verdict,
            "entry_price": None,
            "metadata_json": _candidate_metadata(c, block_reason, run_id),
        })

    for exit_ticker, exit_reason in exit_tickers.items():
        if any(d["ticker"] == exit_ticker and d["gate"] == "buy" for d in decisions):
            continue
        decisions.append({
            "as_of": run_date,
            "scope": scope,
            "ticker": exit_ticker,
            "gate": "sell",
            "verdict": "allow",
            "entry_price": None,
            "metadata_json": _exit_metadata(exit_reason, run_id),
        })

    held_tickers = set()
    if isinstance(holdings, dict):
        held_tickers = set(holdings.keys())
    covered = {d["ticker"] for d in decisions}
    for ticker in held_tickers - covered:
        decisions.append({
            "as_of": run_date,
            "scope": scope,
            "ticker": ticker,
            "gate": "hold",
            "verdict": "allow",
            "entry_price": None,
            "metadata_json": _hold_metadata(
                _finite(scores.get(ticker)), run_id,
            ),
        })

    return decisions


def format_rotation_decisions(
    ctx: Any,
    config: dict[str, Any],
    run_id: str,
    run_date: str,
) -> list[dict[str, Any]]:
    """Extract rotation-pair decisions from the pipeline context."""
    scope = _run_scope(config)
    rotations = getattr(ctx, "rotations", None) or []
    decisions: list[dict[str, Any]] = []
    for r in rotations:
        decisions.append({
            "as_of": run_date,
            "scope": scope,
            "sell_ticker": getattr(r, "sell_ticker", None),
            "buy_ticker": getattr(r, "buy_ticker", None),
            "net_advantage": _finite(getattr(r, "net_advantage", None)),
            "threshold": _finite(getattr(r, "threshold", None)),
            "tax_drag": _finite(getattr(r, "tax_drag", None)),
            "executed": (
                getattr(r, "net_advantage", 0) or 0
            ) >= (getattr(r, "threshold", 0) or 0),
            "run_id": run_id,
        })
    return decisions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_ticker(entry: Any) -> str | None:
    if isinstance(entry, tuple) and len(entry) >= 1:
        return str(entry[0])
    if isinstance(entry, dict):
        return str(entry.get("ticker", ""))
    ticker = getattr(entry, "ticker", None)
    return str(ticker) if ticker else None


def _exit_info(exit_item: Any) -> tuple[str | None, str]:
    if isinstance(exit_item, tuple) and len(exit_item) >= 2:
        ticker = str(exit_item[0])
        sig = exit_item[1]
        reason = getattr(sig, "reason", None) or getattr(sig, "exit_type", None) or str(sig)
        return ticker, str(reason)
    if isinstance(exit_item, dict):
        return exit_item.get("ticker"), exit_item.get("reason", "unknown")
    ticker = getattr(exit_item, "ticker", None)
    reason = getattr(exit_item, "reason", "unknown")
    return (str(ticker) if ticker else None), str(reason)


def _candidate_metadata(
    c: Any, block_reason: str | None, run_id: str,
) -> str:
    import json
    return json.dumps({
        "run_id": run_id,
        "mu": _finite(getattr(c, "mu", None)),
        "sigma": _finite(getattr(c, "sigma", None)),
        "rank_score": _finite(getattr(c, "rank_score", None)),
        "raw_score": _finite(getattr(c, "raw_score", None)),
        "panel_score": _finite(getattr(c, "panel_score", None)),
        "expected_return": _finite(getattr(c, "expected_return", None)),
        "blocked_by": block_reason,
    }, sort_keys=True)


def _exit_metadata(reason: str, run_id: str) -> str:
    import json
    return json.dumps({
        "run_id": run_id,
        "exit_reason": reason,
    }, sort_keys=True)


def _hold_metadata(score: float | None, run_id: str) -> str:
    import json
    return json.dumps({
        "run_id": run_id,
        "score": score,
    }, sort_keys=True)
