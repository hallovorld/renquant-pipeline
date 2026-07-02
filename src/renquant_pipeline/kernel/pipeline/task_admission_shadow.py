"""AdmissionShadowLoggerTask — OBSERVE-ONLY panel-vs-tournament admission shadow.

M5 of the unified-107 master plan (orchestrator
``doc/design/2026-07-02-unified-107-master-plan.md``, Term TC row M5);
lineage: R1 "retire or replace the per-ticker tournament as the
universe-admission gate" in ``doc/design/2026-07-02-104-capability-program.md``
§3.

WHY (the June 2026 tournament-freeze incident): buy admission gates on the
legacy per-ticker tournament artifacts' freshness (LoadUniverseJob →
FilterStalenessTask → ``ctx.models``). The tournament retrain is
timeout-fragile; when it froze (61 days stale by 2026-06-30) the whole book had
0 buy candidates for weeks even though the PANEL scorer's features were
perfectly fresh. R1's replacement rule: a name is admissible iff its features
are fresh and the panel scores it — one model population instead of two.

WHAT THIS TASK DOES: after the live admission and panel scoring have both run,
compute the panel-based admission set alongside the live tournament-admitted
set and append ONE JSONL delta record per session to
``logs/admission_shadow.jsonl`` (default under ``config["_strategy_dir"]``).
Accumulating ≥ 20 sessions of deltas is the evidence substrate for the R1
retirement decision (cut over only when the delta is understood; keep the
tournament read-only for one quarter as rollback).

PANEL-BASED ADMISSIBILITY (per name, decreasing evidence quality):

1. measured YES — the name has a finite score in this run's panel
   cross-section (``ctx._panel_scores_all`` on the kernel/live path,
   ``ctx.panel_scores`` on the lifted load_scorer path);
2. measured NO — the panel machinery explicitly failed the name this run (its
   blocked reason matches ``PANEL_BLOCK_REASON_PREFIXES``), or the whole panel
   fail-closed (``ctx._panel_scoring_contract_failed``) — on a panel
   fail-closed day the panel-based admission set is honestly EMPTY;
3. proxy — the name never reached the panel this run (the tournament rejected
   it upstream, or a non-panel buy gate dropped it before the matrix), so
   "the panel would score it" is inferred from the freshness of the panel's
   own input: the ticker's OHLCV lag vs the session date (fresh within
   ``admission_shadow.max_ohlcv_lag_days``, default 3 calendar days). Records
   carry ``basis="input_fresh_proxy"`` so the R1 analysis can always separate
   measured from inferred admissibility.

Names dropped by NON-panel buy gates (wash-sale, earnings blackout, risk
gates, weak-score vetoes, …) with fresh inputs classify as panel-admissible
via (3): those are decision-funnel outcomes, not admission facts, and must not
flood the delta with noise.

CONTRACT (hard):

* OBSERVE-ONLY / ZERO behavior change — the live admission still rules. This
  task never mutates decision state; it only appends to the JSONL and bumps
  its own counters (``admission_shadow_logged`` / ``admission_shadow_errors``).
* FAIL-ISOLATED — any exception is swallowed, logged, and counted; the run
  proceeds. Flag default-ON is acceptable ONLY because of this property.
  Kill switch: ``config.admission_shadow.enabled = false``.
* APPEND-ONLY JSONL, one self-describing record per run
  (``schema: admission_shadow.v1``).

Ownership note: admission enforcement itself lives in
``kernel/pipeline/job_universe.py`` (per the #210 ownership table). This
module only OBSERVES that outcome (via ``ctx.models`` +
``config["_universe_rejections"]``) against the panel outcome — it must never
grow enforcement behavior.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("kernel.pipeline.admission_shadow")

SCHEMA_VERSION = "admission_shadow.v1"
DEFAULT_LOG_RELPATH = Path("logs") / "admission_shadow.jsonl"
DEFAULT_MAX_OHLCV_LAG_DAYS = 3

# Blocked-map reasons that are PANEL-machinery verdicts (feature row /
# matrix / scorer / calibration failures) — measured "the panel cannot score
# this name". Everything else in the blocked map (wash_sale, earnings_blackout,
# rs_filter, panel_score_below_buy_floor, kelly_zero:*, …) is a decision-funnel
# outcome downstream of admission and must NOT mark a name panel-inadmissible.
PANEL_BLOCK_REASON_PREFIXES: tuple[str, ...] = (
    # kernel/panel_pipeline (live umbrella path)
    "panel_score_missing",
    "panel_scorer_missing",
    "panel_score_matrix_missing",
    "panel_history_",
    "panel_score_collapsed",
    "panel_score_runtime_error",
    "panel_scorer_config_mismatch",
    "panel_scorer_consistency_check_failed",
    # renquant_pipeline.panel_scoring (lifted load_scorer path)
    "missing_panel_score",
    "missing_panel_artifact",
    "missing_panel_feature_contract",
    "missing_feature_row",
    "feature_contract_missing",
    "feature_transform_failed",
    "panel_scorer_load_failed",
    "invalid_global_calibration",
    "missing_global_calibration",
)


# ── Read-only ctx accessors (dual-shape: kernel ctx AND load_scorer ctx) ──────

def _config(ctx: Any) -> dict:
    for attr in ("config", "strategy_config"):
        cfg = getattr(ctx, attr, None)
        if isinstance(cfg, dict):
            return cfg
    return {}


def _shadow_cfg(ctx: Any) -> dict:
    raw = _config(ctx).get("admission_shadow")
    return raw if isinstance(raw, dict) else {}


def _watchlist(ctx: Any) -> list[str]:
    return [str(t) for t in (_config(ctx).get("watchlist") or [])]


def _tournament_set(ctx: Any) -> set[str]:
    """The LIVE admission outcome: LoadUniverseJob → … → ctx.models keys."""
    return {str(t) for t in (getattr(ctx, "models", None) or {})}


def _tournament_rejections(ctx: Any) -> dict[str, str]:
    """Per-name universe rejection reasons (stamped by the universe loader).

    ``live/runner._load_strategy_multi`` writes
    ``config["_universe_rejections"] = dict(uctx.rejections)``; sim/LEAN
    adapters keep the same mapping. Empty dict when the producer predates the
    stamp — reasons then degrade to ``"not_admitted_reason_unavailable"``.
    """
    raw = _config(ctx).get("_universe_rejections") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _finite_panel_scores(ctx: Any) -> dict[str, float]:
    """Names with a FINITE score in this run's panel cross-section.

    Kernel path writes ``ctx._panel_scores_all`` (dict or pandas Series);
    the lifted load_scorer path writes ``ctx.panel_scores`` (dict).
    """
    raw = getattr(ctx, "_panel_scores_all", None)
    if raw is None:
        raw = getattr(ctx, "panel_scores", None)
    out: dict[str, float] = {}
    if raw is None:
        return out
    items = raw.items() if hasattr(raw, "items") else []
    for key, value in items:
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            out[str(key)] = score
    return out


def _blocked_map(ctx: Any) -> dict[str, str]:
    """Read-only union of the per-ticker blocked maps (never created here)."""
    merged: dict[str, str] = {}
    for attr in ("blocked_by", "_blocked_by_ticker"):
        raw = getattr(ctx, attr, None)
        if isinstance(raw, dict):
            for key, value in raw.items():
                merged[str(key)] = str(value)
    return merged


def _held_set(ctx: Any) -> set[str]:
    return {str(t) for t in (getattr(ctx, "holdings", None) or {})}


def _session_date(ctx: Any) -> date | None:
    today = getattr(ctx, "today", None)
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    return None


def _max_ohlcv_lag_days(ctx: Any) -> int:
    try:
        return int(_shadow_cfg(ctx).get(
            "max_ohlcv_lag_days", DEFAULT_MAX_OHLCV_LAG_DAYS,
        ))
    except (TypeError, ValueError):
        return DEFAULT_MAX_OHLCV_LAG_DAYS


def _ohlcv_lag_days(ctx: Any, ticker: str, today: date) -> int | None:
    """Calendar-day lag of the ticker's last OHLCV bar vs the session date.

    None = no usable frame (missing ticker, empty frame, unparseable index).
    This is the same input the live panel builds its features/sequences from
    (``ctx.ohlcv``), which the adapters fetch for the FULL watchlist —
    including tournament-rejected names — so the proxy stays computable in
    exactly the freeze scenario the shadow exists to measure.
    """
    frame = (getattr(ctx, "ohlcv", None) or {}).get(ticker)
    if frame is None:
        return None
    try:
        if len(frame) == 0:
            return None
        last = frame.index.max()
        import pandas as pd  # noqa: PLC0415 — keep module import light

        last_ts = pd.Timestamp(last)
        if pd.isna(last_ts):
            return None
        return (today - last_ts.date()).days
    except Exception:  # noqa: BLE001 — proxy input, not a verdict
        return None


def _is_panel_block_reason(reason: str | None) -> bool:
    return bool(reason) and str(reason).startswith(PANEL_BLOCK_REASON_PREFIXES)


def _classify_panel_admissibility(
    ctx: Any,
    ticker: str,
    panel_scores: dict[str, float],
    blocked: dict[str, str],
    today: date,
    max_lag_days: int,
) -> tuple[bool, str, str]:
    """→ (admissible, basis, reason) per the module-docstring rule."""
    if ticker in panel_scores:
        return True, "panel_scored", "scored"
    reason = blocked.get(ticker)
    if _is_panel_block_reason(reason):
        return False, "panel_block", str(reason)
    lag = _ohlcv_lag_days(ctx, ticker, today)
    if lag is None:
        return False, "input_freshness", "panel_input_missing_ohlcv"
    if lag > max_lag_days:
        return False, "input_freshness", f"panel_input_stale_lag_{lag}d"
    return True, "input_fresh_proxy", f"panel_input_fresh_lag_{lag}d"


def _log_path(ctx: Any) -> Path:
    override = _shadow_cfg(ctx).get("path")
    if override:
        return Path(str(override))
    strategy_dir = _config(ctx).get("_strategy_dir")
    base = Path(str(strategy_dir)) if strategy_dir else Path(".")
    return base / DEFAULT_LOG_RELPATH


def _bump_counter(ctx: Any, key: str) -> None:
    counters = getattr(ctx, "counters", None)
    if isinstance(counters, dict):
        counters[key] = int(counters.get(key, 0)) + 1


# ── The task ──────────────────────────────────────────────────────────────────

class AdmissionShadowLoggerTask:
    """Observe-only, fail-isolated admission-delta logger (M5/R1). See module
    docstring for the full contract; the two invariants are ZERO behavior
    change and NEVER raising out of ``run``."""

    def run(self, ctx: Any) -> None:
        try:
            if not bool(_shadow_cfg(ctx).get("enabled", True)):
                return None
            record = self._build_record(ctx)
            if record is None:
                return None
            self._append(ctx, record)
            _bump_counter(ctx, "admission_shadow_logged")
            log.info(
                "admission shadow: date=%s n_tournament=%d n_panel=%d "
                "added=%d dropped=%d panel_state=%s",
                record["date"], record["n_tournament"], record["n_panel"],
                len(record["added"]), len(record["dropped"]),
                record["panel_state"],
            )
        except Exception:  # noqa: BLE001 — observe-only: NEVER fail the run
            _bump_counter(ctx, "admission_shadow_errors")
            log.exception(
                "admission shadow logger failed — observe-only, run continues",
            )
        return None

    # Internal — everything below runs inside the run() fail-isolation wrap.

    def _build_record(self, ctx: Any) -> dict[str, Any] | None:
        today = _session_date(ctx)
        watchlist = _watchlist(ctx)
        tournament = _tournament_set(ctx)
        if today is None or (not watchlist and not tournament):
            return None   # nothing comparable on this ctx

        panel_scores = _finite_panel_scores(ctx)
        blocked = _blocked_map(ctx)
        rejections = _tournament_rejections(ctx)
        held = _held_set(ctx)
        max_lag_days = _max_ohlcv_lag_days(ctx)

        panel_failed = bool(
            getattr(ctx, "_panel_scoring_contract_failed", False)
        )
        panel_fail_reason = (
            str(getattr(ctx, "_panel_scoring_fail_reason", None) or "unknown")
            if panel_failed else None
        )

        universe = sorted(set(watchlist) | tournament | set(panel_scores))
        panel_admissible: set[str] = set()
        basis_by_name: dict[str, tuple[str, str]] = {}
        for ticker in universe:
            if panel_failed:
                admissible, basis, reason = (
                    False,
                    "panel_fail_closed",
                    f"panel_fail_closed:{panel_fail_reason}",
                )
            else:
                admissible, basis, reason = _classify_panel_admissibility(
                    ctx, ticker, panel_scores, blocked, today, max_lag_days,
                )
            if admissible:
                panel_admissible.add(ticker)
            basis_by_name[ticker] = (basis, reason)

        added = sorted(panel_admissible - tournament)
        dropped = sorted(tournament - panel_admissible)

        reasons: dict[str, dict[str, Any]] = {}
        for ticker in added:
            basis, reason = basis_by_name[ticker]
            reasons[ticker] = {
                "side": "added",
                "tournament": rejections.get(
                    ticker, "not_admitted_reason_unavailable",
                ),
                "panel_basis": basis,
                "panel": reason,
                "held": ticker in held,
            }
        for ticker in dropped:
            basis, reason = basis_by_name[ticker]
            reasons[ticker] = {
                "side": "dropped",
                "tournament": "admitted",
                "panel_basis": basis,
                "panel": reason,
                "held": ticker in held,
            }

        n_proxy = sum(
            1 for t in panel_admissible
            if basis_by_name[t][0] == "input_fresh_proxy"
        )
        run_ts = getattr(ctx, "run_timestamp", None)
        return {
            "schema": SCHEMA_VERSION,
            "date": today.isoformat(),
            "run_timestamp": run_ts.isoformat() if isinstance(
                run_ts, datetime) else None,
            "broker": getattr(ctx, "broker_name", None),
            "run_mode": getattr(ctx, "_run_mode", None),
            "regime": getattr(ctx, "regime", None),
            "panel_state": "fail_closed" if panel_failed else "scored",
            "panel_fail_reason": panel_fail_reason,
            "max_ohlcv_lag_days": max_lag_days,
            "n_watchlist": len(watchlist),
            "n_tournament": len(tournament),
            "n_panel": len(panel_admissible),
            "n_panel_scored": len(set(panel_scores) & panel_admissible),
            "n_panel_proxy": n_proxy,
            "n_intersection": len(tournament & panel_admissible),
            "added": added,
            "dropped": dropped,
            "reasons": reasons,
        }

    def _append(self, ctx: Any, record: dict[str, Any]) -> None:
        path = _log_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
