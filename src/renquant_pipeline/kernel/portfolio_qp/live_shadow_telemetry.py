"""Live-shadow telemetry envelope for QP allocator candidates.

This module is the additive Step 5 surface: it formats incumbent QP and
candidate allocator outputs into the JSONL schema used for live-shadow
operational parity. It does not run allocators and does not emit orders.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (
    check_snapshot_feasibility,
)
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(out):
        return None
    return out


def _array(value: Any, n: int) -> np.ndarray:
    arr = np.asarray(value, dtype=float)
    if arr.shape != (n,):
        raise ValueError(f"expected shape ({n},), got {arr.shape}")
    return arr


def _ticker_map(tickers: Sequence[str], values: np.ndarray) -> dict[str, float | None]:
    return {
        str(ticker): _float(values[idx])
        for idx, ticker in enumerate(tickers)
    }


def _orders_json_safe(orders: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        safe = {}
        for key, value in sorted(order.items()):
            if isinstance(value, (str, int, bool)) or value is None:
                safe[str(key)] = value
            elif isinstance(value, float):
                safe[str(key)] = _float(value)
            else:
                safe[str(key)] = str(value)
        out.append(safe)
    return out


def _path_payload(
    *,
    name: str,
    snap: ConstraintSnapshot,
    solution: Any,
    orders_key: str,
    orders: Sequence[dict[str, Any]] | None,
) -> dict[str, Any]:
    delta_w = _array(getattr(solution, "delta_w"), snap.n)
    target_w = _array(getattr(solution, "target_w"), snap.n)
    return {
        "name": name,
        "status": str(getattr(solution, "status", "")),
        "target_w": _ticker_map(snap.tickers, target_w),
        "delta_w": _ticker_map(snap.tickers, delta_w),
        "n_buys": int((delta_w > 1e-9).sum()),
        "n_sells": int((delta_w < -1e-9).sum()),
        "turnover_l1": _float(np.abs(delta_w).sum()),
        "violations_per_family": check_snapshot_feasibility(
            snap,
            target_w,
            delta_w,
        ),
        orders_key: _orders_json_safe(orders),
    }


def _fingerprint(
    *,
    snap: ConstraintSnapshot,
    mu: Any,
    sigma: Any,
) -> str:
    payload = {
        "contract_version": snap.contract_version,
        "tickers": list(snap.tickers),
        "w_current": _ticker_map(snap.tickers, _array(snap.w_current, snap.n)),
        "w_upper_hard": _ticker_map(snap.tickers, _array(snap.w_upper_hard, snap.n)),
        "mu": _ticker_map(snap.tickers, _array(mu, snap.n)),
        "sigma": _ticker_map(snap.tickers, _array(sigma, snap.n)),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _divergence(
    *,
    snap: ConstraintSnapshot,
    incumbent: Any,
    candidate: Any,
) -> dict[str, Any]:
    inc_target = _array(getattr(incumbent, "target_w"), snap.n)
    cand_target = _array(getattr(candidate, "target_w"), snap.n)
    inc_delta = _array(getattr(incumbent, "delta_w"), snap.n)
    cand_delta = _array(getattr(candidate, "delta_w"), snap.n)

    inc_names = {snap.tickers[i] for i in np.where(np.abs(inc_target) > 1e-9)[0]}
    cand_names = {snap.tickers[i] for i in np.where(np.abs(cand_target) > 1e-9)[0]}
    union = inc_names | cand_names
    active_delta = np.where((np.abs(inc_delta) > 1e-9) | (np.abs(cand_delta) > 1e-9))[0]
    if len(active_delta):
        sign_agree = float(
            np.mean(np.sign(inc_delta[active_delta]) == np.sign(cand_delta[active_delta]))
        )
    else:
        sign_agree = 1.0
    return {
        "abs_target_w_l1": _float(np.abs(inc_target - cand_target).sum()),
        "ticker_overlap_pct": 1.0 if not union else len(inc_names & cand_names) / len(union),
        "delta_w_sign_agreement_pct": sign_agree,
        "would_be_friendly_fire": [
            snap.tickers[i]
            for i in range(snap.n)
            if cand_delta[i] < -1e-9 and inc_delta[i] > 1e-9
        ],
        "would_be_missed_alpha": [
            snap.tickers[i]
            for i in range(snap.n)
            if cand_delta[i] > 1e-9 and inc_delta[i] <= 1e-9
        ],
    }


def _anomalies(
    incumbent: Any,
    candidate: Any,
    extra: Sequence[str] | None,
) -> list[str]:
    out = list(extra or [])
    for prefix, solution in (
        ("incumbent", incumbent),
        ("candidate", candidate),
    ):
        status = str(getattr(solution, "status", ""))
        if status.startswith("infeasible"):
            out.append(f"{prefix}_status={status}")
        if status == "cap_compliance_fallback":
            out.append(f"{prefix}_qp_used_cap_compliance_fallback")
    return out


def build_live_shadow_telemetry_envelope(
    *,
    snap: ConstraintSnapshot,
    mu: Any,
    sigma: Any,
    incumbent_solution: Any,
    candidate_solution: Any,
    candidate_name: str,
    as_of_date: str | dt.date,
    as_of_time: str | dt.datetime | None = None,
    broker: str = "alpaca-paper",
    incumbent_name: str = "current_qp",
    live_orders_emitted: Sequence[dict[str, Any]] | None = None,
    would_have_orders: Sequence[dict[str, Any]] | None = None,
    broker_fidelity: dict[str, Any] | None = None,
    regime: str | None = None,
    regime_confidence: float | None = None,
    panel_artifact: str | None = None,
    anomalies: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe live-shadow telemetry envelope."""
    as_of_date_s = as_of_date.isoformat() if hasattr(as_of_date, "isoformat") else str(as_of_date)
    if as_of_time is None:
        as_of_time_s = None
    else:
        as_of_time_s = as_of_time.isoformat() if hasattr(as_of_time, "isoformat") else str(as_of_time)
    incumbent = _path_payload(
        name=incumbent_name,
        snap=snap,
        solution=incumbent_solution,
        orders_key="live_orders_emitted",
        orders=live_orders_emitted,
    )
    candidate = _path_payload(
        name=candidate_name,
        snap=snap,
        solution=candidate_solution,
        orders_key="would_have_orders",
        orders=would_have_orders,
    )
    return {
        "schema_version": "qp-live-shadow-v1",
        "as_of_date": as_of_date_s,
        "as_of_time": as_of_time_s,
        "broker": broker,
        "incumbent_name": incumbent_name,
        "candidate_name": candidate_name,
        "constraint_snapshot_contract_version": snap.contract_version,
        "ctx_fingerprint": _fingerprint(snap=snap, mu=mu, sigma=sigma),
        "incumbent": incumbent,
        "candidate": candidate,
        "divergence": _divergence(
            snap=snap,
            incumbent=incumbent_solution,
            candidate=candidate_solution,
        ),
        "broker_fidelity": dict(broker_fidelity or {}),
        "regime": regime or snap.regime,
        "regime_confidence": _float(regime_confidence if regime_confidence is not None else snap.confidence),
        "panel_artifact": panel_artifact,
        "anomalies": _anomalies(incumbent_solution, candidate_solution, anomalies),
    }


def append_live_shadow_telemetry_jsonl(
    path: str | Path,
    envelope: dict[str, Any],
) -> Path:
    """Append one live-shadow telemetry envelope to a JSONL file."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n")
    return out


__all__ = [
    "append_live_shadow_telemetry_jsonl",
    "build_live_shadow_telemetry_envelope",
]
