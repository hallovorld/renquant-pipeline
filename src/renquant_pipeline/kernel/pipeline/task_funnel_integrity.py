"""FunnelIntegrityTask — first-class session verdict on the buy funnel.

Operator mandate (2026-07-11, after the 07-08/09 admission outage): the
silent-no-buy class — an ENGINEERING block masquerading as a normal
"no trade (no_candidates)" — must be solved by a pipeline step, not by
after-the-fact forensics. Two sessions of zero buy capability (133/145
admission models stale → buy scan ran on 0 tickers) were reported through
the exact same quiet ntfy path as an ordinary weak-signal day. See
renquant-orchestrator PR #473 (META no-buy forensics) for the incident
record; the recommendations there ("page on buy-scan universe collapse as
an OUTAGE") are consumed downstream of this task's output contract.

WHAT THIS TASK DOES: at the END of every full InferencePipeline run it
classifies the session's buy-funnel outcome into a first-class verdict and
publishes a ``funnel_integrity`` block on the run context:

  * ``ECONOMIC_TRADE``    — buys were emitted; no integrity invariant fired.
  * ``ECONOMIC_NO_TRADE`` — zero buys, and every no-buy is accounted for by
    correctly-scaled economic/risk bars (no invariant fired). This is the
    only verdict under which "no trade" may be reported as normal.
  * ``DEGRADED``          — invariant(s) fired but buy capability partially
    survived (buys still emitted), or only warn-severity findings fired.
  * ``STRUCTURAL_BLOCK``  — a structural invariant fired AND zero buys were
    emitted: an engineering condition suppressed capability. Downstream
    notification must title this an OUTAGE, never a no-trade.

STRUCTURAL DETECTORS (each a named invariant; thresholds config-keyed under
``config["funnel_integrity"][<invariant>]`` with safe defaults):

  * ``universe_admission_collapse`` — admitted universe below a floor
    fraction of the watchlist, or staleness/data-cutoff rejections above a
    fraction of the watchlist (the 07-08/09 signature:
    ``stale_76d_limit_60:live_train_end`` × 133/145).
  * ``single_gate_funnel_kill``     — one gate family eliminated 100% (config
    share) of the assembled (late-funnel) candidates, leaving zero, when
    session history says that gate rarely fires. Cold-start (insufficient
    history) downgrades to warn severity.
  * ``threshold_scale_mismatch``    — the conviction ``mu_floor`` exceeds the
    max achievable mu this session (structural when every mu is ≤ 0 — the
    PatchTST-all-negative-scores signature; warn when positive-but-below), or
    the rotation threshold exceeds the max ER while rotation emitted nothing.
    These mechanize the decision-tree-review checklist items.
  * ``fail_close_event``            — panel/calibrator fingerprint fail-close
    state (``panel_scorer_config_mismatch``, calibrator mismatch, …)
    surfaced as candidate-clears this run.
  * ``wash_sale_mass_block``        — wash-sale kills above both an absolute
    floor and the historical p99 (the STATE-EXT-SELL date-bug signature:
    reconciliation stamps "today" → mass §1091 blocking).
  * ``zero_priced_candidates``      — an abnormal fraction of the buy-relevant
    universe has zero/missing prices or no OHLCV frame.

PLUG-IN CONTRACT (for the retrospective-sweep registry): an invariant is any
object with a stable ``name: str`` and
``evaluate(view: FunnelView, cfg: dict) -> InvariantFinding | None``.
Additional incident classes plug in via ``FunnelIntegrityTask(invariants=…)``
or by extending ``DEFAULT_INVARIANTS`` in a follow-up PR; per-invariant kill
switch: ``funnel_integrity.<name>.enabled = false``.

OUTPUT CONTRACT (``ctx.funnel_integrity``, schema ``funnel_integrity.v1``):

  ``schema, date, run_mode, verdict, verdict_reason, structural, fired[]
  (invariant / severity / reason / evidence), invariants_evaluated[],
  gate_kill_counts{family: n}, funnel{n_watchlist, n_admitted,
  n_universe_rejected, n_buy_scan_blocked, n_late_candidates,
  n_candidates_final, n_ranked, n_rotations, n_buy_orders, n_exits,
  buy_blocked, bear_only, skip_buys}, error``

Downstream persistence stamps this block into the run bundle verbatim under
the key ``funnel_integrity``; the umbrella ntfy path titles the run via
``notification_headline(getattr(ctx, "funnel_integrity", None))`` →
``{"outage": bool, "title_tag": "OUTAGE"|"DEGRADED"|"NO-TRADE"|"TRADE"|
"UNKNOWN", "line": str}``. Integer mirrors land on ``ctx.counters``
(``funnel_integrity_structural`` / ``funnel_integrity_fired`` /
``funnel_integrity_errors``) so the existing counters_json persistence
carries the headline even before any consumer reads the block.

CONTRACT (hard, same isolation pattern as AdmissionShadowLoggerTask):

  * OBSERVE-ONLY / ZERO behavior change — no signal, decision, sizing or
    order state is mutated. The task only reads funnel state and writes its
    own block + counters + its rolling history slice of ``ctx.monitor_state``.
  * FAIL-ISOLATED — any exception inside the task is swallowed, logged and
    counted; a crash here must NEVER dark a run. When the crash happens
    after partial assembly the published block carries ``error`` so the
    integrity signal itself is never silently absent.
  * Default-ON is acceptable ONLY because of the isolation property.
    Kill switch: ``funnel_integrity.enabled = false``.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

log = logging.getLogger("kernel.pipeline.funnel_integrity")

SCHEMA_VERSION = "funnel_integrity.v1"
CTX_ATTR = "funnel_integrity"

VERDICT_ECONOMIC_TRADE = "ECONOMIC_TRADE"
VERDICT_ECONOMIC_NO_TRADE = "ECONOMIC_NO_TRADE"
VERDICT_DEGRADED = "DEGRADED"
VERDICT_STRUCTURAL_BLOCK = "STRUCTURAL_BLOCK"

SEVERITY_STRUCTURAL = "structural"
SEVERITY_WARN = "warn"

HISTORY_STATE_KEY = "funnel_integrity_history"
DEFAULT_HISTORY_WINDOW = 60

# Universe-rejection reason prefixes that mean "the admission gate refused
# the artifact on freshness/cutoff grounds" (kernel/pipeline/job_universe.py
# FilterStalenessTask taxonomy). The 07-08/09 outage reason was
# ``stale_76d_limit_60:live_train_end``.
STALENESS_REJECTION_PREFIXES: tuple[str, ...] = (
    "stale_",
    "data_cutoff_missing",
    "data_cutoff_unparseable",
    "data_cutoff_future",
)

# Blocked-map reasons that are fingerprint / fail-close verdicts — the
# machinery refused to score, which must surface as an integrity event, not
# as a quiet candidate-clear. (Narrower than the admission shadow's
# PANEL_BLOCK_REASON_PREFIXES: per-name feature gaps are routine; these are
# contract failures.)
FAIL_CLOSE_BLOCK_REASON_PREFIXES: tuple[str, ...] = (
    "panel_scorer_config_mismatch",
    "panel_scorer_consistency_check_failed",
    "panel_scorer_load_failed",
    "panel_score_runtime_error",
    "invalid_global_calibration",
    "missing_global_calibration",
    "missing_panel_artifact",
    "missing_panel_feature_contract",
    "feature_contract_missing",
)


# ── Findings + read-only funnel view ─────────────────────────────────────────

@dataclass(frozen=True)
class InvariantFinding:
    """One fired invariant: a named engineering condition with evidence."""

    invariant: str
    severity: str                       # SEVERITY_STRUCTURAL | SEVERITY_WARN
    reason: str                         # one-line human-readable diagnosis
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "invariant": self.invariant,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FunnelView:
    """Read-only snapshot of the session's buy funnel, built once from ctx.

    Detectors consume this instead of the raw ctx so (a) they cannot mutate
    decision state by construction, and (b) the retrospective-sweep registry
    can replay persisted sessions through the same detectors.
    """

    today: date | None
    config: dict
    watchlist: tuple[str, ...]
    admitted: frozenset[str]            # LoadUniverseJob outcome (ctx.models)
    universe_rejections: dict           # ticker → rejection reason
    blocked: dict                       # ticker → buy-funnel kill reason
    gate_kill_counts: dict              # gate family → kill count
    late_candidate_tickers: tuple[str, ...]   # assembled candidates (pre-veto)
    session_mus: dict                   # ticker → calibrated expected_return
    n_candidates_final: int
    n_ranked: int
    n_rotations: int
    n_buy_orders: int
    n_exits: int
    buy_blocked: bool
    bear_only: bool
    skip_buys: bool
    panel_fail_closed: bool
    panel_fail_reason: str | None
    calibrator_fail_closed: bool
    prices: dict
    ohlcv_tickers: frozenset[str]
    holdings: frozenset[str]
    counters: dict
    history: tuple[dict, ...]           # prior sessions (today excluded)


# ── ctx accessors (read-only; tolerant of SimpleNamespace fixtures) ──────────

def _config(ctx: Any) -> dict:
    cfg = getattr(ctx, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _fi_cfg(ctx_or_config: Any) -> dict:
    cfg = ctx_or_config if isinstance(ctx_or_config, dict) else _config(ctx_or_config)
    raw = cfg.get("funnel_integrity")
    return raw if isinstance(raw, dict) else {}


def invariant_cfg(config: dict, name: str) -> dict:
    raw = _fi_cfg(config).get(name)
    return raw if isinstance(raw, dict) else {}


def _session_date(ctx: Any) -> date | None:
    today = getattr(ctx, "today", None)
    if isinstance(today, datetime):
        return today.date()
    if isinstance(today, date):
        return today
    return None


def _blocked_map(ctx: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for attr in ("blocked_by", "_blocked_by_ticker"):
        raw = getattr(ctx, attr, None)
        if isinstance(raw, dict):
            for key, value in raw.items():
                merged[str(key)] = str(value)
    return merged


def gate_family(reason: str) -> str:
    """Normalize a kill reason to its gate family (prefix before ':').

    ``wash_sale:npv_cost_...`` → ``wash_sale``; ``conviction:mu_below_floor``
    → ``conviction``; ``veto:rank_score_below_floor`` → ``veto``.
    """
    return str(reason).split(":", 1)[0].strip() or "unknown"


def _session_mus(ctx: Any) -> dict[str, float]:
    """ticker → calibrated expected_return, union of every session surface.

    Sources (increasing precedence): per-ticker score snapshots
    (``ctx._ticker_score_snapshot`` — survives blocking), the pre-veto full
    candidate snapshot (``ctx._full_candidate_snapshot``), and the surviving
    ``ctx.candidates``.
    """
    out: dict[str, float] = {}
    snaps = getattr(ctx, "_ticker_score_snapshot", None)
    if isinstance(snaps, dict):
        for ticker, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            er = snap.get("expected_return")
            try:
                er_f = float(er)
            except (TypeError, ValueError):
                continue
            if math.isfinite(er_f):
                out[str(ticker)] = er_f
    for attr in ("_full_candidate_snapshot", "candidates"):
        for cand in (getattr(ctx, attr, None) or []):
            ticker = getattr(cand, "ticker", None)
            er = getattr(cand, "expected_return", None)
            if ticker is None:
                continue
            try:
                er_f = float(er)
            except (TypeError, ValueError):
                continue
            if math.isfinite(er_f):
                out[str(ticker)] = er_f
    return out


def _prior_history(ctx: Any, today_iso: str) -> list[dict]:
    state = getattr(ctx, "monitor_state", None)
    raw = state.get(HISTORY_STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(raw, list):
        return []
    return [
        h for h in raw
        if isinstance(h, dict) and h.get("date") != today_iso
    ]


def _wash_sale_count(blocked: dict[str, str], counters: dict) -> int:
    from_map = sum(
        1 for reason in blocked.values() if gate_family(reason) == "wash_sale"
    )
    try:
        from_counter = int(counters.get("blocked_wash", 0))
    except (TypeError, ValueError):
        from_counter = 0
    return max(from_map, from_counter)


def build_funnel_view(ctx: Any) -> FunnelView:
    config = _config(ctx)
    today = _session_date(ctx)
    today_iso = today.isoformat() if today else ""
    blocked = _blocked_map(ctx)
    kill_counts: dict[str, int] = {}
    for reason in blocked.values():
        family = gate_family(reason)
        kill_counts[family] = kill_counts.get(family, 0) + 1
    rejections_raw = config.get("_universe_rejections") or {}
    rejections = (
        {str(k): str(v) for k, v in rejections_raw.items()}
        if isinstance(rejections_raw, dict) else {}
    )
    late = tuple(
        str(getattr(c, "ticker", ""))
        for c in (getattr(ctx, "_full_candidate_snapshot", None) or [])
        if getattr(c, "ticker", None)
    )
    counters = getattr(ctx, "counters", None)
    counters = counters if isinstance(counters, dict) else {}
    prices_raw = getattr(ctx, "prices", None)
    return FunnelView(
        today=today,
        config=config,
        watchlist=tuple(str(t) for t in (config.get("watchlist") or [])),
        admitted=frozenset(str(t) for t in (getattr(ctx, "models", None) or {})),
        universe_rejections=rejections,
        blocked=blocked,
        gate_kill_counts=kill_counts,
        late_candidate_tickers=late,
        session_mus=_session_mus(ctx),
        n_candidates_final=len(getattr(ctx, "candidates", None) or []),
        n_ranked=len(getattr(ctx, "ranked", None) or []),
        n_rotations=len(getattr(ctx, "rotations", None) or []),
        n_buy_orders=len(getattr(ctx, "orders", None) or []),
        n_exits=len(getattr(ctx, "exits", None) or []),
        buy_blocked=bool(getattr(ctx, "buy_blocked", False)),
        bear_only=bool(getattr(ctx, "bear_only", False)),
        skip_buys=bool(getattr(ctx, "skip_buys", False)),
        panel_fail_closed=bool(
            getattr(ctx, "_panel_scoring_contract_failed", False)
        ),
        panel_fail_reason=(
            str(getattr(ctx, "_panel_scoring_fail_reason", None))
            if getattr(ctx, "_panel_scoring_fail_reason", None) is not None
            else None
        ),
        calibrator_fail_closed=bool(
            getattr(ctx, "_calibrator_contract_failed", False)
        ),
        prices=(dict(prices_raw) if isinstance(prices_raw, dict) else {}),
        ohlcv_tickers=frozenset(
            str(t) for t in (getattr(ctx, "ohlcv", None) or {})
        ),
        holdings=frozenset(
            str(t) for t in (getattr(ctx, "holdings", None) or {})
        ),
        counters=counters,
        history=tuple(_prior_history(ctx, today_iso)),
    )


# ── Structural detectors (named invariants) ──────────────────────────────────

def _f(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _i(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


class UniverseAdmissionCollapseInvariant:
    """Admitted universe collapsed vs the watchlist (07-08/09 signature).

    Fires when the watchlist is non-empty AND either
      * admitted count < ``min_admitted_frac`` (default 0.5) × watchlist, or
      * staleness/data-cutoff rejections > ``max_staleness_rejection_frac``
        (default 0.5) × watchlist.
    """

    name = "universe_admission_collapse"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        n_watch = len(view.watchlist)
        if n_watch == 0:
            return None
        min_frac = _f(cfg, "min_admitted_frac", 0.5)
        max_stale_frac = _f(cfg, "max_staleness_rejection_frac", 0.5)
        n_admitted = len(view.admitted)
        n_stale = sum(
            1 for reason in view.universe_rejections.values()
            if str(reason).startswith(STALENESS_REJECTION_PREFIXES)
        )
        admitted_low = n_admitted < min_frac * n_watch
        stale_high = n_stale > max_stale_frac * n_watch
        if not (admitted_low or stale_high):
            return None
        histogram: dict[str, int] = {}
        for reason in view.universe_rejections.values():
            key = gate_family(reason)
            histogram[key] = histogram.get(key, 0) + 1
        top = sorted(histogram.items(), key=lambda kv: -kv[1])[:3]
        return InvariantFinding(
            invariant=self.name,
            severity=SEVERITY_STRUCTURAL,
            reason=(
                f"universe admission collapsed: {n_admitted}/{n_watch} "
                f"admitted, {n_stale} staleness rejections"
            ),
            evidence={
                "n_watchlist": n_watch,
                "n_admitted": n_admitted,
                "n_universe_rejected": len(view.universe_rejections),
                "n_staleness_rejections": n_stale,
                "min_admitted_frac": min_frac,
                "max_staleness_rejection_frac": max_stale_frac,
                "admitted_below_floor": admitted_low,
                "staleness_above_threshold": stale_high,
                "top_rejection_reasons": dict(top),
            },
        )


class SingleGateFunnelKillInvariant:
    """One gate family killed 100% of assembled candidates → zero survivors.

    Evaluated over the LATE funnel (``_full_candidate_snapshot``: names that
    were fully assembled as candidates), so routine early-scan kills
    (``model_signal:hold`` mass-holds) never dilute the share. History
    (rolling ``monitor_state`` slice) gates severity: a gate that history
    says rarely fires is structural; with insufficient history the finding
    downgrades to warn.
    """

    name = "single_gate_funnel_kill"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        if view.n_candidates_final > 0:
            return None
        late = [t for t in view.late_candidate_tickers if t in view.blocked]
        min_kills = _i(cfg, "min_kills", 3)
        if len(late) < min_kills:
            return None
        min_share = _f(cfg, "min_share", 1.0)
        families: dict[str, int] = {}
        for ticker in late:
            family = gate_family(view.blocked[ticker])
            families[family] = families.get(family, 0) + 1
        top_family, top_count = max(families.items(), key=lambda kv: kv[1])
        share = top_count / len(late)
        if share < min_share:
            return None
        min_history = _i(cfg, "min_history_sessions", 10)
        rare_rate = _f(cfg, "rare_fire_rate", 0.25)
        n_hist = len(view.history)
        fired_days = sum(
            1 for h in view.history
            if top_family in (h.get("kill_families") or [])
        )
        hist_rate = (fired_days / n_hist) if n_hist else None
        if n_hist >= min_history and hist_rate is not None \
                and hist_rate >= rare_rate:
            return None    # history says this gate fires routinely — economic
        severity = (
            SEVERITY_STRUCTURAL if n_hist >= min_history else SEVERITY_WARN
        )
        return InvariantFinding(
            invariant=self.name,
            severity=severity,
            reason=(
                f"gate family '{top_family}' killed {top_count}/{len(late)} "
                f"assembled candidates (share {share:.0%}); zero survived"
            ),
            evidence={
                "gate_family": top_family,
                "killed": top_count,
                "n_late_candidates": len(late),
                "share": share,
                "min_share": min_share,
                "history_sessions": n_hist,
                "history_fire_rate": hist_rate,
                "rare_fire_rate": rare_rate,
                "history_basis": (
                    "sufficient" if n_hist >= min_history else "insufficient"
                ),
                "family_kill_counts": families,
            },
        )


class ThresholdScaleMismatchInvariant:
    """Conviction/rotation bars set above the session's achievable scale.

    Mechanizes the decision-tree-review checklist items:
      * ``conviction_gate.mu_floor`` > max achievable mu this session —
        STRUCTURAL when every session mu ≤ 0 (the PatchTST-negative-scores
        signature: an absolute floor above an all-negative scale can never
        admit), WARN when the max is positive but below the floor.
      * rotation ``min_expected_advantage_pct`` > max session ER while
        rotation is enabled and emitted nothing — WARN (net advantage can
        still exceed a candidate's absolute ER against a negative-ER hold).
    """

    name = "threshold_scale_mismatch"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        checks: dict[str, dict[str, Any]] = {}
        severity: str | None = None
        reasons: list[str] = []

        mus = list(view.session_mus.values())
        conv_cfg = (
            (view.config.get("ranking", {}) or {})
            .get("panel_scoring", {})
            .get("conviction_gate")
            or {}
        )
        if conv_cfg.get("enabled") and conv_cfg.get("mu_floor") is not None \
                and mus:
            mu_floor = _f(conv_cfg, "mu_floor", 0.0)
            max_mu = max(mus)
            xs_mean = (
                (sum(mus) / len(mus))
                if conv_cfg.get("demean_cross_sectional") else 0.0
            )
            achievable = max_mu - xs_mean
            if achievable < mu_floor:
                sev = (
                    SEVERITY_STRUCTURAL if max_mu <= 0.0 else SEVERITY_WARN
                )
                checks["conviction"] = {
                    "mu_floor": mu_floor,
                    "max_mu": max_mu,
                    "xs_mean": xs_mean,
                    "max_achievable_mu": achievable,
                    "all_mus_nonpositive": max_mu <= 0.0,
                    "severity": sev,
                }
                severity = _max_severity(severity, sev)
                reasons.append(
                    f"conviction mu_floor {mu_floor:+.4f} > max achievable "
                    f"mu {achievable:+.4f} (session max mu {max_mu:+.4f})"
                )

        rot_cfg = view.config.get("rotation", {}) or {}
        rot_threshold = rot_cfg.get("min_expected_advantage_pct")
        if rot_cfg.get("enabled") and rot_threshold is not None \
                and view.n_rotations == 0 and mus:
            threshold = _f(rot_cfg, "min_expected_advantage_pct", 0.0)
            max_er = max(mus)
            if max_er < threshold:
                checks["rotation"] = {
                    "min_expected_advantage_pct": threshold,
                    "max_expected_return": max_er,
                    "severity": SEVERITY_WARN,
                }
                severity = _max_severity(severity, SEVERITY_WARN)
                reasons.append(
                    f"rotation threshold {threshold:+.4f} > max session ER "
                    f"{max_er:+.4f}"
                )

        if not checks:
            return None
        return InvariantFinding(
            invariant=self.name,
            severity=severity or SEVERITY_WARN,
            reason="; ".join(reasons),
            evidence={"checks": checks, "n_session_mus": len(mus)},
        )


class FailCloseEventInvariant:
    """Fingerprint / fail-close machinery state surfaced as candidate-clears.

    Fires on the book-wide panel fail-closed flag, the calibrator fail-closed
    flag, or any per-name kill whose reason is a contract-failure verdict
    (``panel_scorer_config_mismatch`` & co). A fail-closed day whose only
    external symptom is "no trade" is exactly the shadow config-FP incident
    (dark for 3 days, 2026-06); this invariant makes it first-class.
    """

    name = "fail_close_event"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        hit_reasons: dict[str, int] = {}
        for reason in view.blocked.values():
            if str(reason).startswith(FAIL_CLOSE_BLOCK_REASON_PREFIXES):
                key = gate_family(reason)
                hit_reasons[key] = hit_reasons.get(key, 0) + 1
        if not (view.panel_fail_closed or view.calibrator_fail_closed
                or hit_reasons):
            return None
        parts: list[str] = []
        if view.panel_fail_closed:
            parts.append(
                f"panel fail-closed ({view.panel_fail_reason or 'unknown'})"
            )
        if view.calibrator_fail_closed:
            parts.append("calibrator fail-closed")
        if hit_reasons:
            parts.append(
                f"{sum(hit_reasons.values())} candidate-clears with "
                f"fail-close reasons"
            )
        return InvariantFinding(
            invariant=self.name,
            severity=SEVERITY_STRUCTURAL,
            reason="; ".join(parts),
            evidence={
                "panel_fail_closed": view.panel_fail_closed,
                "panel_fail_reason": view.panel_fail_reason,
                "calibrator_fail_closed": view.calibrator_fail_closed,
                "fail_close_clears": hit_reasons,
            },
        )


class WashSaleMassBlockInvariant:
    """Wash-sale kills anomalously above history (STATE-EXT-SELL signature).

    Fires when this session's wash-sale kill count is ≥ ``min_count``
    (default 5) AND — once ≥ ``min_history_sessions`` (default 10) of history
    exist — strictly above the historical p99 of per-session wash-sale kill
    counts. Cold start falls back to the absolute floor alone.
    """

    name = "wash_sale_mass_block"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        count = _wash_sale_count(view.blocked, view.counters)
        min_count = _i(cfg, "min_count", 5)
        if count < min_count:
            return None
        min_history = _i(cfg, "min_history_sessions", 10)
        hist_counts = [
            int(h.get("wash_sale_blocked", 0) or 0) for h in view.history
        ]
        p99 = _p99(hist_counts) if len(hist_counts) >= min_history else None
        if p99 is not None and count <= p99:
            return None
        return InvariantFinding(
            invariant=self.name,
            severity=SEVERITY_STRUCTURAL,
            reason=(
                f"wash-sale blocked {count} names this session "
                f"(min_count={min_count}, historical p99="
                f"{p99 if p99 is not None else 'insufficient-history'})"
            ),
            evidence={
                "wash_sale_blocked": count,
                "min_count": min_count,
                "historical_p99": p99,
                "history_sessions": len(hist_counts),
                "history_basis": (
                    "sufficient" if p99 is not None else "insufficient"
                ),
            },
        )


class ZeroPricedCandidatesInvariant:
    """Abnormal fraction of the buy-relevant universe zero-priced/data-less.

    Buy-relevant = admitted ∪ assembled candidates, minus holdings. A name
    offends when the session price map has it ≤ 0, or its OHLCV frame is
    absent. Each leg is evaluated only when its source map is populated at
    all (an adapter that never fills ``ctx.prices`` must not fire this).
    """

    name = "zero_priced_candidates"

    def evaluate(self, view: FunnelView, cfg: dict) -> InvariantFinding | None:
        relevant = sorted(
            (set(view.admitted) | set(view.late_candidate_tickers))
            - set(view.holdings)
        )
        if not relevant:
            return None
        max_frac = _f(cfg, "max_frac", 0.2)
        min_count = _i(cfg, "min_count", 3)
        offenders: dict[str, str] = {}
        for ticker in relevant:
            if view.prices:
                price = view.prices.get(ticker)
                try:
                    price_f = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price_f = None
                if price_f is None or not math.isfinite(price_f) \
                        or price_f <= 0.0:
                    offenders[ticker] = "zero_or_missing_price"
                    continue
            if view.ohlcv_tickers and ticker not in view.ohlcv_tickers:
                offenders[ticker] = "missing_ohlcv"
        if not view.prices and not view.ohlcv_tickers:
            return None
        frac = len(offenders) / len(relevant)
        if len(offenders) < min_count or frac < max_frac:
            return None
        sample = dict(sorted(offenders.items())[:10])
        return InvariantFinding(
            invariant=self.name,
            severity=SEVERITY_STRUCTURAL,
            reason=(
                f"{len(offenders)}/{len(relevant)} buy-relevant names "
                f"zero-priced or missing data ({frac:.0%})"
            ),
            evidence={
                "n_offenders": len(offenders),
                "n_relevant": len(relevant),
                "frac": frac,
                "max_frac": max_frac,
                "min_count": min_count,
                "sample": sample,
            },
        )


def _max_severity(current: str | None, new: str) -> str:
    order = {SEVERITY_WARN: 0, SEVERITY_STRUCTURAL: 1}
    if current is None or order[new] > order[current]:
        return new
    return current


def _p99(counts: list[int]) -> int | None:
    if not counts:
        return None
    ordered = sorted(counts)
    idx = max(0, math.ceil(0.99 * len(ordered)) - 1)
    return ordered[idx]


DEFAULT_INVARIANTS: tuple[Any, ...] = (
    UniverseAdmissionCollapseInvariant(),
    SingleGateFunnelKillInvariant(),
    ThresholdScaleMismatchInvariant(),
    FailCloseEventInvariant(),
    WashSaleMassBlockInvariant(),
    ZeroPricedCandidatesInvariant(),
)


# ── Verdict + notification contract ──────────────────────────────────────────

def classify_verdict(
    view: FunnelView, findings: list[InvariantFinding],
) -> tuple[str, str]:
    structural = [f for f in findings if f.severity == SEVERITY_STRUCTURAL]
    buys = view.n_buy_orders > 0
    if structural and not buys:
        names = ", ".join(f.invariant for f in structural)
        return (
            VERDICT_STRUCTURAL_BLOCK,
            f"zero buys with structural invariant(s) fired: {names}",
        )
    if findings:
        names = ", ".join(f.invariant for f in findings)
        return (
            VERDICT_DEGRADED,
            f"invariant(s) fired ({names}) but capability partially "
            f"survived (buys={view.n_buy_orders})",
        )
    if buys:
        return VERDICT_ECONOMIC_TRADE, f"{view.n_buy_orders} buy order(s) emitted"
    return (
        VERDICT_ECONOMIC_NO_TRADE,
        "no invariant fired; no-buy is accounted for by economic/risk bars "
        f"(final candidates={view.n_candidates_final}, "
        f"buy_blocked={view.buy_blocked})",
    )


def notification_headline(block: dict | None) -> dict[str, Any]:
    """Notification-contract adapter for the umbrella ntfy path.

    Consumes ``getattr(ctx, "funnel_integrity", None)`` and returns
    ``{"outage": bool, "title_tag": str, "line": str}``:

      * ``outage``    — True iff verdict == STRUCTURAL_BLOCK: the run must be
        titled an OUTAGE (engineering block), never a quiet no-trade.
      * ``title_tag`` — OUTAGE | DEGRADED | NO-TRADE | TRADE | UNKNOWN.
      * ``line``      — one-line body segment (verdict + fired invariants).
    """
    if not isinstance(block, dict) or not block.get("verdict"):
        return {
            "outage": False,
            "title_tag": "UNKNOWN",
            "line": "funnel integrity: not evaluated",
        }
    verdict = str(block["verdict"])
    tag = {
        VERDICT_STRUCTURAL_BLOCK: "OUTAGE",
        VERDICT_DEGRADED: "DEGRADED",
        VERDICT_ECONOMIC_NO_TRADE: "NO-TRADE",
        VERDICT_ECONOMIC_TRADE: "TRADE",
    }.get(verdict, "UNKNOWN")
    fired = block.get("fired") or []
    fired_names = ", ".join(
        str(f.get("invariant", "?")) for f in fired if isinstance(f, dict)
    )
    line = f"funnel integrity: {verdict}"
    if fired_names:
        line += f" [{fired_names}]"
    error = block.get("error")
    if error:
        line += f" (integrity-task error: {error})"
    return {"outage": tag == "OUTAGE", "title_tag": tag, "line": line}


# ── The task ──────────────────────────────────────────────────────────────────

class FunnelIntegrityTask:
    """Observe-only, fail-isolated buy-funnel integrity verdict (see module
    docstring). The two hard invariants are ZERO behavior change and NEVER
    raising out of ``run`` — a crash here must never dark the run it audits."""

    def __init__(self, invariants: tuple[Any, ...] | None = None) -> None:
        self._invariants = tuple(invariants or DEFAULT_INVARIANTS)

    def run(self, ctx: Any) -> None:
        try:
            fi_cfg = _fi_cfg(ctx)
            if not bool(fi_cfg.get("enabled", True)):
                return None
            if getattr(ctx, "_run_mode", None) == "sell-only":
                return None    # exit-only variant has no buy funnel to judge
            block = self._build_block(ctx)
            setattr(ctx, CTX_ATTR, block)
            self._mirror_counters(ctx, block)
            self._update_history(ctx, block)
            log.info(
                "funnel integrity: verdict=%s fired=%d structural=%s "
                "candidates_final=%d buys=%d",
                block["verdict"], len(block["fired"]), block["structural"],
                block["funnel"]["n_candidates_final"],
                block["funnel"]["n_buy_orders"],
            )
            if block["verdict"] == VERDICT_STRUCTURAL_BLOCK:
                log.warning(
                    "FunnelIntegrityAlert: STRUCTURAL_BLOCK — engineering "
                    "condition suppressed buy capability; do NOT report this "
                    "session as a normal no-trade. fired=%s",
                    [f["invariant"] for f in block["fired"]],
                )
        except Exception as exc:  # noqa: BLE001 — observe-only: NEVER fail the run
            self._record_error(ctx, exc)
        return None

    # Internal — everything below runs inside the run() fail-isolation wrap.

    def _build_block(self, ctx: Any) -> dict[str, Any]:
        view = build_funnel_view(ctx)
        findings: list[InvariantFinding] = []
        evaluated: list[str] = []
        detector_errors: list[str] = []
        for invariant in self._invariants:
            name = str(getattr(invariant, "name", type(invariant).__name__))
            inv_cfg = invariant_cfg(view.config, name)
            if not bool(inv_cfg.get("enabled", True)):
                continue
            evaluated.append(name)
            try:
                finding = invariant.evaluate(view, inv_cfg)
            except Exception as exc:  # noqa: BLE001 — one detector's crash
                # must neither dark the run nor take the OTHER detectors dark.
                detector_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                log.exception(
                    "funnel-integrity detector %r raised — skipped, "
                    "run unaffected", name,
                )
                continue
            if finding is not None:
                findings.append(finding)
        verdict, verdict_reason = classify_verdict(view, findings)
        return {
            "schema": SCHEMA_VERSION,
            "date": view.today.isoformat() if view.today else None,
            "run_mode": getattr(ctx, "_run_mode", None),
            "verdict": verdict,
            "verdict_reason": verdict_reason,
            "structural": any(
                f.severity == SEVERITY_STRUCTURAL for f in findings
            ),
            "fired": [f.as_dict() for f in findings],
            "invariants_evaluated": evaluated,
            "gate_kill_counts": dict(view.gate_kill_counts),
            "funnel": {
                "n_watchlist": len(view.watchlist),
                "n_admitted": len(view.admitted),
                "n_universe_rejected": len(view.universe_rejections),
                "n_buy_scan_blocked": len(view.blocked),
                "n_late_candidates": len(view.late_candidate_tickers),
                "n_candidates_final": view.n_candidates_final,
                "n_ranked": view.n_ranked,
                "n_rotations": view.n_rotations,
                "n_buy_orders": view.n_buy_orders,
                "n_exits": view.n_exits,
                "buy_blocked": view.buy_blocked,
                "bear_only": view.bear_only,
                "skip_buys": view.skip_buys,
            },
            "error": ("; ".join(detector_errors) or None),
        }

    def _mirror_counters(self, ctx: Any, block: dict[str, Any]) -> None:
        counters = getattr(ctx, "counters", None)
        if not isinstance(counters, dict):
            return
        counters["funnel_integrity_fired"] = len(block["fired"])
        counters["funnel_integrity_structural"] = int(block["structural"])

    def _update_history(self, ctx: Any, block: dict[str, Any]) -> None:
        """Append this session's compact record to the rolling history.

        Lives on ``ctx.monitor_state`` (adapter-persisted across bars, same
        vehicle as MonitorIdleStreakTask). One record per trading day —
        a re-run replaces the same-date entry, so intraday repeats cannot
        inflate history.
        """
        state = getattr(ctx, "monitor_state", None)
        if not isinstance(state, dict):
            return
        today_iso = block.get("date")
        if not today_iso:
            return
        window = _i(_fi_cfg(ctx), "history_window", DEFAULT_HISTORY_WINDOW)
        prior = _prior_history(ctx, today_iso)
        record = {
            "date": today_iso,
            "kill_families": sorted(
                {
                    gate_family(r)
                    for r in _blocked_map(ctx).values()
                }
            ),
            "wash_sale_blocked": _wash_sale_count(
                _blocked_map(ctx),
                getattr(ctx, "counters", None) or {},
            ),
            "verdict": block["verdict"],
        }
        state[HISTORY_STATE_KEY] = (prior + [record])[-max(window, 1):]

    def _record_error(self, ctx: Any, exc: Exception) -> None:
        log.exception(
            "FunnelIntegrityTask failed — observe-only, run continues",
        )
        try:
            counters = getattr(ctx, "counters", None)
            if isinstance(counters, dict):
                counters["funnel_integrity_errors"] = (
                    int(counters.get("funnel_integrity_errors", 0)) + 1
                )
            existing = getattr(ctx, CTX_ATTR, None)
            if not isinstance(existing, dict):
                today = _session_date(ctx)
                setattr(ctx, CTX_ATTR, {
                    "schema": SCHEMA_VERSION,
                    "date": today.isoformat() if today else None,
                    "run_mode": getattr(ctx, "_run_mode", None),
                    "verdict": None,
                    "verdict_reason": None,
                    "structural": False,
                    "fired": [],
                    "invariants_evaluated": [],
                    "gate_kill_counts": {},
                    "funnel": {},
                    "error": f"{type(exc).__name__}: {exc}",
                })
        except Exception:  # noqa: BLE001 — even error handling must not raise
            log.exception("FunnelIntegrityTask error-handler failed")
