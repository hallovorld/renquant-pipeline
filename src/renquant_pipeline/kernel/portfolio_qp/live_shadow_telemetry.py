"""Live-shadow telemetry envelope for QP allocator candidates.

This module is the additive Step 5 surface: it formats incumbent QP and
candidate allocator outputs into the JSONL schema used for live-shadow
operational parity. It does not run allocators and does not emit orders.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (
    check_snapshot_feasibility,
)
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.pipeline.pipeline import Task

log = logging.getLogger("kernel.portfolio_qp.live_shadow_telemetry")


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


def load_live_shadow_telemetry_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load live-shadow telemetry JSONL rows."""
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


def _mean(values: Sequence[float | None]) -> float | None:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return None
    return float(np.mean(cleaned))


def summarize_live_shadow_telemetry(
    rows: Sequence[dict[str, Any]],
    *,
    shadow_days_needed: int = 30,
) -> dict[str, Any]:
    """Summarize QP live-shadow JSONL rows for promotion review."""
    anomaly_counts = Counter(
        str(anomaly)
        for row in rows
        for anomaly in (row.get("anomalies") or [])
    )
    days = sorted({str(row.get("as_of_date")) for row in rows if row.get("as_of_date")})
    candidate_statuses = [
        str((row.get("candidate") or {}).get("status") or "")
        for row in rows
    ]
    incumbent_statuses = [
        str((row.get("incumbent") or {}).get("status") or "")
        for row in rows
    ]
    divergence_rows = [row.get("divergence") or {} for row in rows]
    snapshot_invalid = int(anomaly_counts.get("qp_constraint_snapshot_invalid", 0))
    return {
        "schema_version": "qp-live-shadow-summary-v1",
        "source_schema_version": "qp-live-shadow-v1",
        "n_rows": len(rows),
        "incumbent": str(rows[0].get("incumbent_name") or "unknown") if rows else None,
        "candidate": str(rows[0].get("candidate_name") or "unknown") if rows else None,
        "shadow_days_logged": len(days),
        "shadow_days_needed": int(shadow_days_needed),
        "dates": days,
        "metrics": {
            "abs_target_w_l1_mean": _mean([
                _float(row.get("abs_target_w_l1"))
                for row in divergence_rows
            ]),
            "ticker_overlap_pct_mean": _mean([
                _float(row.get("ticker_overlap_pct"))
                for row in divergence_rows
            ]),
            "delta_w_sign_agreement_pct_mean": _mean([
                _float(row.get("delta_w_sign_agreement_pct"))
                for row in divergence_rows
            ]),
            "incumbent_qp_fallback_fired_pct": (
                None if not rows else sum(
                    status == "cap_compliance_fallback"
                    for status in incumbent_statuses
                ) / len(rows)
            ),
            "candidate_infeasible_pct": (
                None if not rows else sum(
                    status.startswith("infeasible")
                    for status in candidate_statuses
                ) / len(rows)
            ),
        },
        "anomaly_count_by_type": dict(sorted(anomaly_counts.items())),
        "promotion_gate": {
            "ready_for_review": len(days) >= int(shadow_days_needed) and snapshot_invalid == 0,
            "snapshot_invalid_count": snapshot_invalid,
        },
    }


def write_live_shadow_summary_json(
    *,
    input_jsonl: str | Path,
    output_json: str | Path,
    shadow_days_needed: int = 30,
) -> Path:
    """Write a summary JSON for a QP live-shadow telemetry JSONL file."""
    summary = summarize_live_shadow_telemetry(
        load_live_shadow_telemetry_jsonl(input_jsonl),
        shadow_days_needed=shadow_days_needed,
    )
    out = Path(output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


class EmitQPLiveShadowTelemetryTask(Task):
    """Emit readonly QP live-shadow telemetry after live QP order emission.

    Disabled by default. When enabled, the task runs a configured candidate
    allocator on the already-built ``ConstraintSnapshot`` and writes one JSONL
    row comparing it with the live incumbent QP solution. It never mutates
    ``ctx.orders`` / ``ctx.exits`` and never changes the incumbent solution.
    """

    name = "EmitQPLiveShadowTelemetryTask"

    def run(self, ctx) -> bool | None:
        cfg = self._config(ctx)
        if not cfg.get("enabled", False):
            return None

        try:
            snap = getattr(ctx, "_qp_constraint_snapshot", None)
            incumbent = getattr(ctx, "_qp_solution", None)
            mu = getattr(ctx, "_qp_mu", None)
            sigma = getattr(ctx, "_qp_sigma", None)
            if snap is None or incumbent is None or mu is None or sigma is None:
                self._stamp_skip(ctx, "missing_qp_shadow_inputs")
                return None

            candidate_name = str(cfg.get("candidate_name") or "hybrid_option_f_allocator")
            candidate = _run_candidate_allocator(
                candidate_name,
                snap=snap,
                mu=mu,
                sigma=sigma,
                Sigma=getattr(ctx, "_qp_Sigma_full", None),
            )
            envelope = build_live_shadow_telemetry_envelope(
                snap=snap,
                mu=mu,
                sigma=sigma,
                incumbent_solution=incumbent,
                candidate_solution=candidate,
                candidate_name=candidate_name,
                as_of_date=getattr(ctx, "today", None) or dt.date.today(),
                as_of_time=dt.datetime.now(dt.timezone.utc),
                broker=str(getattr(ctx, "broker_name", None) or cfg.get("broker") or "unknown"),
                incumbent_name=str(cfg.get("incumbent_name") or "current_qp"),
                live_orders_emitted=_live_qp_orders(ctx),
                would_have_orders=_would_have_weight_intents(snap, candidate),
                broker_fidelity=dict(getattr(ctx, "_broker_fidelity", {}) or {}),
                regime=getattr(ctx, "regime", None),
                regime_confidence=getattr(ctx, "confidence", None),
                panel_artifact=_panel_artifact_id(ctx),
                anomalies=list(getattr(ctx, "_qp_shadow_anomalies", []) or []),
            )
            path = self._resolve_path(ctx, cfg)
            append_live_shadow_telemetry_jsonl(path, envelope)
            ctx._qp_live_shadow_telemetry_path = str(path)  # noqa: SLF001
            ctx._qp_live_shadow_telemetry_last = envelope  # noqa: SLF001
            ctx._qp_live_shadow_telemetry_status = "written"  # noqa: SLF001
            _inc_counter(ctx, "qp_live_shadow_telemetry_rows", 1)
        except Exception as exc:  # noqa: BLE001
            self._stamp_skip(ctx, f"qp_live_shadow_telemetry_error:{type(exc).__name__}")
            log.exception("EmitQPLiveShadowTelemetryTask: failed")
        return None

    @staticmethod
    def _config(ctx) -> dict[str, Any]:
        joint = ((getattr(ctx, "config", None) or {}).get("rotation", {})
                 .get("joint_actions", {}) or {})
        nested = joint.get("qp_live_shadow_telemetry") or {}
        if not isinstance(nested, dict):
            nested = {}
        return {
            "enabled": bool(
                nested.get(
                    "enabled",
                    joint.get("qp_live_shadow_telemetry_enabled", False),
                )
            ),
            "candidate_name": (
                nested.get("candidate_name")
                or nested.get("candidate")
                or joint.get("qp_live_shadow_candidate")
                or "hybrid_option_f_allocator"
            ),
            "path": (
                nested.get("path")
                or nested.get("jsonl_path")
                or joint.get("qp_live_shadow_jsonl_path")
            ),
            "broker": nested.get("broker") or joint.get("qp_live_shadow_broker"),
            "incumbent_name": (
                nested.get("incumbent_name")
                or joint.get("qp_live_shadow_incumbent_name")
                or "current_qp"
            ),
        }

    @staticmethod
    def _resolve_path(ctx, cfg: dict[str, Any]) -> Path:
        raw = cfg.get("path")
        if raw:
            path = Path(str(raw))
        else:
            path = Path("artifacts/live-shadow/qp-live-shadow.jsonl")
        if path.is_absolute():
            return path
        root = (
            getattr(ctx, "strategy_dir", None)
            or (getattr(ctx, "config", None) or {}).get("_strategy_dir")
            or "."
        )
        return Path(root) / path

    @staticmethod
    def _stamp_skip(ctx, reason: str) -> None:
        ctx._qp_live_shadow_telemetry_status = reason  # noqa: SLF001
        _inc_counter(ctx, "qp_live_shadow_telemetry_skipped", 1)


def _run_candidate_allocator(
    name: str,
    *,
    snap: ConstraintSnapshot,
    mu: Any,
    sigma: Any,
    Sigma: np.ndarray | None,
) -> Any:
    from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import (  # noqa: PLC0415
        equal_weight_top_k,
        fractional_kelly_top_k,
        hard_only_qp_allocator,
        hybrid_option_f_allocator,
        inverse_vol_top_k,
    )

    registry = {
        "equal_weight_top_k": lambda: equal_weight_top_k(snap, mu=mu),
        "inverse_vol_top_k": lambda: inverse_vol_top_k(snap, mu=mu, sigma=sigma),
        "fractional_kelly_top_k": lambda: fractional_kelly_top_k(
            snap, mu=mu, sigma=sigma,
        ),
        "hybrid_option_f_allocator": lambda: hybrid_option_f_allocator(
            snap, mu=mu, sigma=sigma, Sigma=Sigma,
        ),
        "hard_only_qp_allocator": lambda: hard_only_qp_allocator(
            snap, mu=mu, sigma=sigma, Sigma=Sigma,
        ),
    }
    if name not in registry:
        raise KeyError(
            f"unknown qp live-shadow candidate {name!r}; "
            f"registered={sorted(registry)}"
        )
    return registry[name]()


def _would_have_weight_intents(
    snap: ConstraintSnapshot,
    solution: Any,
    *,
    tol: float = 1e-9,
) -> list[dict[str, Any]]:
    delta_w = _array(getattr(solution, "delta_w"), snap.n)
    target_w = _array(getattr(solution, "target_w"), snap.n)
    out: list[dict[str, Any]] = []
    for idx, ticker in enumerate(snap.tickers):
        dw = float(delta_w[idx])
        if abs(dw) <= tol:
            continue
        out.append({
            "ticker": str(ticker),
            "side": "buy" if dw > 0 else "sell",
            "delta_w": _float(dw),
            "target_w": _float(target_w[idx]),
            "source": "qp_live_shadow",
        })
    return out


def _live_qp_orders(ctx) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in getattr(ctx, "orders", None) or []:
        if not isinstance(order, dict):
            continue
        if (
            order.get("source_job") == "JointPortfolioQPJob"
            or str(order.get("order_type", "")).startswith("QP_")
            or order.get("source") == "qp"
        ):
            out.append(order)
    for item in getattr(ctx, "exits", None) or []:
        try:
            ticker, signal = item
        except (TypeError, ValueError):
            continue
        if getattr(signal, "source_job", None) != "JointPortfolioQPJob":
            continue
        out.append({
            "ticker": ticker,
            "side": "sell",
            "quantity": _float(getattr(signal, "quantity", None)),
            "exit_type": getattr(signal, "exit_type", None),
            "reason": getattr(signal, "reason", None),
            "source_job": getattr(signal, "source_job", None),
            "source_task": getattr(signal, "source_task", None),
            "decision_inputs": getattr(signal, "decision_inputs", None),
        })
    return out


def _panel_artifact_id(ctx) -> str | None:
    active = getattr(ctx, "_active_panel_scorer", None)
    if isinstance(active, dict):
        value = active.get("artifact_path") or active.get("artifact_id")
        if value:
            return str(value)
    manifest = getattr(ctx, "artifact_manifest", None)
    if isinstance(manifest, dict):
        value = manifest.get("artifact_id") or manifest.get("uri")
        if value:
            return str(value)
    return None


def _inc_counter(ctx, key: str, amount: int) -> None:
    counters = getattr(ctx, "counters", None)
    if counters is None:
        counters = {}
        ctx.counters = counters
    counters[key] = int(counters.get(key, 0)) + int(amount)


__all__ = [
    "EmitQPLiveShadowTelemetryTask",
    "append_live_shadow_telemetry_jsonl",
    "build_live_shadow_telemetry_envelope",
    "load_live_shadow_telemetry_jsonl",
    "summarize_live_shadow_telemetry",
    "write_live_shadow_summary_json",
]
