"""Vol-window buy license — the CONFIRMED vol-switch conditional as a mechanism.

Lineage (renquant-orchestrator): design ``doc/design/2026-08-18-vol-window-
shadow-first.md`` (#1004, approved), authorized by the CONFIRMED vol-switch
confirmatory verdict ``doc/research/2026-08-18-vol-switch-results.md`` (#1003)
under the frozen prereg (#1001). This module implements design §2/§3 impl PR 1:
the license mechanism, behind a lane-scoped config flag.

WHAT IT IS: when trailing market volatility is elevated (ON) and the day is not
BEAR, the panel's top-decile selection carries a certified positive spread
(ON mean +0.184/60d, NW t +1.952, boot q05 +0.021, P2 block-t +2.378
[VERIFIED — prior work, orch#1003 results §1]). Inside that window — and ONLY
inside it — this license substitutes for the missing per-regime WF admission
evidence in ``RegimeModelAdmissionTask``: the top-decile (by served panel
score) names stay buy-admissible; every downstream protection (sizing, caps,
cash floor, tax, wash-sale, QP) applies unchanged. Outside the window, and in
every config that does not explicitly enable the flag, behavior is
byte-identical to today.

FROZEN WINDOW SEMANTICS (design §2 — no re-derivation here):

* ON at date d  ⇔  SPY 20-trading-day realized vol (close-to-close simple
  returns, sample std ddof=1, annualized sqrt(252)) > 0.135 — STRICT, so
  exactly 0.135 is OFF [VERIFIED — prior work, orch#1001 prereg §2 frozen
  constant; orch#1003 §1 ran it as the decisive fixed definition]. Computed
  PIT from the serving SPY series the regime tasks already consume
  (``ctx.spy_returns`` — the same access path as ``BEAROverrideTask``),
  read-only.
* Window = ON ∧ ¬BEAR: the hard-BEAR override retains ABSOLUTE precedence.
  If the day is BEAR (``ctx.regime == BEAR`` or ``regime_state.hard_bear``),
  the license never activates. Additionally (fail-closed narrowing, never
  widening): the license requires a RESOLVED non-BEAR regime from the
  enumerated set below — an unknown/unresolved regime is refused because it
  carries no BEAR-precedence information.
* Top decile: N = int(round(n/10)) names by served panel score, descending
  [VERIFIED — prior work, orch#1003 runner ``top_decile_spread``]. Ties break
  deterministically on ticker (the runner used the stable panel row order;
  ticker order is the runtime-deterministic equivalent — declared operational
  deviation, recorded in the session row via ``tie_break``).

HARD CONTRACT:

* UNREACHABLE WITHOUT THE FLAG — :func:`evaluate_vol_window_license` returns
  ``None`` before touching ``ctx`` unless
  ``ranking.panel_scoring.vol_window_license.enabled`` is EXACTLY ``true``.
  No prod config carries the key, so prod lanes are byte-identical
  (test-proven: ``tests/test_vol_window_license.py``).
* SUBSTITUTES ONLY for the missing per-regime WF evidence refusals
  (``regime_admission:*`` from trade-monotonicity / sanity-IC). It can NEVER
  override the diagnostic-only governance refusal (`diagnostic_only_ok` is a
  hard input), never removes a protection, never relaxes a cap, never touches
  the sell side.
* FAIL-CLOSED — any missing/degenerate input (short SPY history, NaN vol,
  empty score cross-section, unresolved regime) yields an INACTIVE license
  (today's blocked behavior), never a spurious activation.
* KILL SWITCH — the lane-scoped env ``RENQUANT_VOL_WINDOW_LICENSE_DISABLE``
  (any non-empty value except ``0``/``false``/``no``/``off``) forces the
  license inactive while still emitting the session row (design AC4).
* SESSION LEDGER — when (and only when) the flag is enabled, one JSONL row
  per session is appended (``logs/vol_window_license.jsonl`` under the
  strategy dir, mirroring the ``admission_shadow`` / ``parking_sleeve``
  lane-log convention): window state (vol20, threshold verdict, BEAR override
  state), the licensed names, and the underlying refusal. Would-be orders
  after the full funnel live in the lane's existing runs-DB/decision-ledger
  persistence; the AC2/AC3 readout that joins them with realized h=60 (and
  diagnostic h=20) outcomes is impl PR 2 (design §7). The ledger write is
  fail-isolated: it can never fail the run, and a write failure never flips
  the license decision.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("kernel.panel_pipeline.vol_window_license")

SCHEMA_VERSION = "vol_window_license.v1"

# Frozen design constants (orch#1001 prereg §2 / orch#1003 §1; orch#1004 §2).
# The lane config declares the same values explicitly (runner-guards-are-
# prereg-content); these defaults exist so a lane config that omits one still
# runs the certified definition, never a drifted one.
DEFAULT_VOL_WINDOW_DAYS = 20
DEFAULT_ON_THRESHOLD = 0.135
ANNUALIZATION_DAYS = 252.0
TOP_DECILE_DIVISOR = 10

KILL_SWITCH_ENV = "RENQUANT_VOL_WINDOW_LICENSE_DISABLE"
DEFAULT_LEDGER_RELPATH = Path("logs") / "vol_window_license.jsonl"

# Fail-closed regime allow-list (enumerated-allowlist lesson applied in the
# fail-CLOSED direction): the license may only fire in a RESOLVED non-BEAR
# regime. Anything else — BEAR, UNKNOWN, empty, a future label this module
# has never seen — refuses. This can only narrow the design's ON ∧ ¬BEAR
# window, never widen it.
NON_BEAR_REGIMES = frozenset({"BULL_CALM", "BULL_VOLATILE", "CHOPPY", "BULL_STRONG"})


def kill_switch_engaged(environ: Any = None) -> bool:
    """True when the lane-scoped kill-switch env forces the license OFF."""
    env = os.environ if environ is None else environ
    raw = str(env.get(KILL_SWITCH_ENV, "") or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def spy_realized_vol(spy_returns: Any, window: int = DEFAULT_VOL_WINDOW_DAYS) -> float | None:
    """Trailing ``window``-day realized vol of the serving SPY return series.

    Certified construction [VERIFIED — prior work, orch#1003 runner
    ``realized_vol20``]: close-to-close simple returns, sample std (ddof=1),
    annualized sqrt(252). ``spy_returns`` is the same series the regime tasks
    consume (``ctx.spy_returns`` — built from close.pct_change() by the
    serving adapters); only the LAST ``window`` observations are read, so the
    value at a session is a pure function of data at or before that session
    (PIT — no lookahead by construction; test-pinned).

    Returns ``None`` (→ license inactive, fail-closed) on short history or
    any non-finite input.
    """
    try:
        values = [float(v) for v in list(spy_returns or [])[-int(window):]]
    except (TypeError, ValueError):
        return None
    if len(values) < int(window) or int(window) < 2:
        return None
    if any(not math.isfinite(v) for v in values):
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)  # ddof=1
    vol = math.sqrt(var) * math.sqrt(ANNUALIZATION_DAYS)
    return vol if math.isfinite(vol) else None


def top_decile_by_score(scores: Any) -> tuple[list[str], dict[str, Any]]:
    """Top decile of the served panel cross-section, certified construction.

    N = int(round(n/10)) names by score descending [VERIFIED — prior work,
    orch#1003 runner ``top_decile_spread``]; ties break deterministically on
    ticker (declared operational deviation from the runner's panel-row-order
    stable sort — both are deterministic). Non-finite scores are excluded
    from n and from membership. Empty/degenerate cross-sections yield an
    empty decile (→ license inactive, fail-closed).
    """
    finite: dict[str, float] = {}
    items = scores.items() if hasattr(scores, "items") else []
    for ticker, value in items:
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            finite[str(ticker)] = score
    n = len(finite)
    n_decile = int(round(n / TOP_DECILE_DIVISOR))
    info: dict[str, Any] = {
        "universe_n": n,
        "top_decile_n": 0,
        "top_decile_score_floor": None,
        "tie_break": "ticker",
    }
    if n_decile < 1:
        return [], info
    ranked = sorted(finite.items(), key=lambda kv: (-kv[1], kv[0]))[:n_decile]
    info["top_decile_n"] = len(ranked)
    info["top_decile_score_floor"] = ranked[-1][1]
    return [t for t, _ in ranked], info


def _session_date(ctx: Any) -> str | None:
    today = getattr(ctx, "today", None)
    if isinstance(today, datetime.datetime):
        return today.date().isoformat()
    if isinstance(today, datetime.date):
        return today.isoformat()
    return None


def _bump(ctx: Any, key: str) -> None:
    counters = getattr(ctx, "counters", None)
    if isinstance(counters, dict):
        counters[key] = int(counters.get(key, 0)) + 1


def evaluate_vol_window_license(
    ctx: Any,
    panel_cfg: dict,
    *,
    diagnostic_only_ok: bool,
    admission_ok: bool,
    base_reason: str | None,
    note: str | None = None,
) -> dict[str, Any] | None:
    """Evaluate the vol-window license for this session.

    Returns ``None`` — before reading ANYTHING off ``ctx`` — unless
    ``panel_cfg["vol_window_license"]["enabled"] is True``. That early return
    is the unreachability guarantee for every config without the flag.

    Otherwise returns the full session record (see module docstring), with
    ``license_applied`` True iff every leg holds:
    window ON (strict vol20 > threshold) ∧ resolved non-BEAR regime ∧ no
    hard-BEAR override ∧ kill switch disengaged ∧ the diagnostic-only stage
    passed ∧ the underlying admission REFUSED (there is a missing-evidence
    slot to substitute for) ∧ the top decile is non-empty.
    """
    cfg = panel_cfg.get("vol_window_license") if isinstance(panel_cfg, dict) else None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None
    return _evaluate_enabled(
        ctx,
        cfg,
        diagnostic_only_ok=bool(diagnostic_only_ok),
        admission_ok=bool(admission_ok),
        base_reason=base_reason,
        note=note,
    )


def _evaluate_enabled(
    ctx: Any,
    cfg: dict,
    *,
    diagnostic_only_ok: bool,
    admission_ok: bool,
    base_reason: str | None,
    note: str | None = None,
) -> dict[str, Any]:
    try:
        window_days = int(cfg.get("vol_window_days", DEFAULT_VOL_WINDOW_DAYS))
    except (TypeError, ValueError):
        window_days = DEFAULT_VOL_WINDOW_DAYS
    try:
        threshold = float(cfg.get("threshold", DEFAULT_ON_THRESHOLD))
    except (TypeError, ValueError):
        threshold = DEFAULT_ON_THRESHOLD

    regime = str(getattr(ctx, "regime", "") or "UNKNOWN")
    hard_bear = bool(getattr(getattr(ctx, "regime_state", None), "hard_bear", False))
    regime_resolved_non_bear = regime in NON_BEAR_REGIMES
    bear_blocked = hard_bear or not regime_resolved_non_bear

    vol = spy_realized_vol(getattr(ctx, "spy_returns", None), window_days)
    vol_on = (vol is not None) and (vol > threshold)  # STRICT: == threshold is OFF

    killed = kill_switch_engaged()

    top_decile, decile_info = top_decile_by_score(
        getattr(ctx, "_panel_scores_all", None) or {}
    )
    window_on = bool(vol_on and not bear_blocked and not killed)
    applied = bool(
        window_on
        and diagnostic_only_ok
        and not admission_ok
        and top_decile,
    )

    candidate_tickers = []
    for cand in list(getattr(ctx, "candidates", []) or []):
        ticker = getattr(cand, "ticker", None)
        if ticker:
            candidate_tickers.append(str(ticker))
    holdings = getattr(ctx, "holdings", {}) or {}
    top_set = set(top_decile)

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "date": _session_date(ctx),
        "lane_tag": os.environ.get("RENQUANT_READONLY_TAG"),
        # Window state (design AC2: vol20 value, threshold verdict, BEAR
        # override state).
        "vol_window_days": window_days,
        "vol20": vol,
        "threshold": threshold,
        "vol_verdict_on": bool(vol_on),
        "regime": regime,
        "hard_bear": hard_bear,
        "regime_resolved_non_bear": regime_resolved_non_bear,
        "bear_precedence_blocked": bool(bear_blocked),
        "kill_switch": bool(killed),
        "window_on": window_on,
        # Admission interplay.
        "diagnostic_only_ok": bool(diagnostic_only_ok),
        "admission_ok": bool(admission_ok),
        "base_reason": base_reason,
        "license_applied": applied,
        # Selection (design AC2: licensed names; funnel outcomes live in the
        # lane's existing runs-DB persistence — joined by the PR-2 readout).
        "top_decile": sorted(top_decile),
        "licensed_candidates": sorted(top_set & set(candidate_tickers)) if applied else [],
        "licensed_holdings": sorted(top_set & {str(t) for t in holdings}) if applied else [],
        "n_candidates_at_admission": len(candidate_tickers),
        **decile_info,
    }
    if note is not None:
        record["note"] = note
    return record


def emit_session_record(ctx: Any, panel_cfg: dict, record: dict[str, Any]) -> None:
    """Append the session row to the lane's JSONL ledger. Fail-isolated:
    exceptions are swallowed + counted; the write NEVER changes the license
    decision (the record is already final when this is called)."""
    try:
        cfg = panel_cfg.get("vol_window_license") or {}
        override = cfg.get("ledger_path")
        if override:
            path = Path(str(override))
        else:
            strategy_dir = (getattr(ctx, "config", {}) or {}).get("_strategy_dir")
            base = Path(str(strategy_dir)) if strategy_dir else Path(".")
            path = base / DEFAULT_LEDGER_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _bump(ctx, "vol_window_ledger_logged")
    except Exception:  # noqa: BLE001 — observe-only sink: never fail the run
        _bump(ctx, "vol_window_ledger_errors")
        log.exception("vol-window ledger write failed — run continues")
