"""Eligibility-ledger precondition for the VetoWeakBuys small-n guard.

Normative spec: ``doc/design/2026-07-18-smalln-guard-eligibility-ledger.md``
(r2 amendment to RFC 2026-07-17, approved pipeline #207) plus the approving
review's two implementation expectations:

(a) POLICY set-identity assertion — for every POLICY-classed exclusion
    reason present in the scan, the set of names tagged with the EXACT
    declared reason string must equal ``watchlist − config-declared
    eligible set``; any mismatch → NOT CLEAN (a record-full misapplication
    of pinned config is invisible to every count predicate, but POLICY
    narrowing is completely determined by reviewed config, so set identity
    closes it).
(b) config-frozen watchlist outer anchor — ``watchlist_size`` is carried in
    the §3 persistence block so a shrunken eligibility computation cannot
    hide the outage that produced it.

Amendment §2 — CLEAN means ALL of:

1. **Mass balance.** Every name in ``expected_universe`` (a counter + ticker
   list EMITTED BY THE CANDIDATE-GENERATION STAGE, recorded before any
   drop; see :func:`emit_expected_universe`) is accounted for as either a
   surviving candidate at the floor or a per-name recorded exclusion. An
   unaccounted shortfall (bars-feed outage / June per-ticker-staleness
   shape) or an ABSENT counter (older pipeline) → NOT CLEAN.
2. **Funnel integrity.** ``panel_score_missing == 0`` AND no NaN/inf
   rank_score among current candidates.
3. **Approved-normal reasons with share bounds.** Every pre-floor exclusion
   reason belongs to the explicit allowlist frozen in config
   (``ranking.panel_scoring.smalln_eligibility``); each INTEGRITY-classed
   reason's count share of ``expected_universe`` must be within its
   config-frozen bound (defaults: wash-sale ≤ 20%, realized-vol ≤ 50%,
   corporate action ≤ 10%). POLICY-classed narrowing is exempt from share
   bounds but must satisfy the exact-string + set-identity assertion (a).
   An unknown/unclassifiable reason → NOT CLEAN.
4. **No failure markers.** v1 detectable set (all existing surfaces):
   ``ctx._panel_scoring_contract_failed`` (its reason string carries the
   fingerprint-dispatch route on fingerprint mismatches),
   ``ctx._calibrator_contract_failed`` (ditto for calibrator-side
   fingerprint dispatch), ``panel_score_missing`` counters,
   ``veto:rank_score_nan`` (both re-checked here as condition 2), and the
   NEW ``ctx._feed_staleness_flagged`` marker (the fundamentals-staleness
   warning promoted to a machine surface by this amendment).

NOT CLEAN on a small-n day → the relax-only branch MUST NOT act: the floor
stays status quo (fails toward no-entry) and the run is tagged
``smalln_guard_suppressed(reason=<first failing class>)`` at ERROR — the
orchestrator degradation sentinel alarms on that tag (amendment §2 named
deliverable, extending orchestrator #545).

The partition is computed on EVERY session (normal-n included) and attached
to ``ctx._smalln_eligibility`` as the schema-versioned §3 block, plus
submitted to the gate registry (verdict ``allow`` — pure observability; the
lattice max can never be raised by an allow) so the adapter's existing
``record_gate_verdicts`` / decision-ledger write paths persist it without
any orchestrator-side coupling.

Persistence contract (for the run-bundle writer, orchestrator-side):
``ctx._smalln_eligibility`` is a plain JSON-serializable dict with
``schema_version`` = :data:`SMALLN_LEDGER_SCHEMA_VERSION`. Older pipelines
never set the attribute; consumers must treat absence as explicit
(``smalln_ledger: absent`` per amendment §3).

Known accounting gap (stated, not hidden): ``PostStopCooldownFilterTask``
(opt-in, default OFF) drops candidates with a counter but no per-name
blocked record; on a day it fires at small n those names are unaccounted →
NOT CLEAN (fail-closed by design — a reason surface that wants approval
must first record per-name evidence and then be allowlisted in config).
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger("kernel.panel.job_panel_scoring")

#: Bump on any structural change to the ctx._smalln_eligibility block.
SMALLN_LEDGER_SCHEMA_VERSION = 1

#: Counter name emitted by the candidate-generation stage (amendment §2
#: condition 1 — REQUIRED new instrumentation; absent → NOT CLEAN).
EXPECTED_UNIVERSE_COUNTER = "expected_universe"

#: Ticker list companion to the counter (set accounting + set identity).
EXPECTED_UNIVERSE_TICKERS_ATTR = "_smalln_expected_universe_tickers"

#: The LOUD suppression tag (grep-able; the sentinel patterns on it).
SUPPRESSION_TAG = "smalln_guard_suppressed"

#: ctx attribute carrying the §3 persistence block.
LEDGER_ATTR = "_smalln_eligibility"

#: Config sub-block under ranking.panel_scoring (frozen keys).
CONFIG_KEY = "smalln_eligibility"

#: §2 condition 3 default INTEGRITY share bounds (share of expected_universe).
DEFAULT_INTEGRITY_SHARE_BOUNDS = {
    "wash_sale": 0.20,
    "realized_vol": 0.50,
    "corporate_action": 0.10,
}

#: Exact reason string → INTEGRITY class (live funnel surfaces).
_INTEGRITY_REASON_EXACT = {
    "risk_gate_vol": "realized_vol",
    "earnings_blackout": "corporate_action",
}

#: Reason prefix → INTEGRITY class (reasons carrying a detail suffix).
_INTEGRITY_REASON_PREFIX = {
    "wash_sale:": "wash_sale",
}

#: Within-scan drop reasons: part of funnel accounting (condition 2), not
#: pre-floor exclusions (condition 3). ``_drop_unscored_panel_candidates``
#: tags with exactly this string at both call sites.
_SCAN_DROP_REASONS = frozenset({"panel_score_missing"})

# branch_action vocabulary (§3): acted / not_small_n / suppressed:<reason>
# / deconfigured. Implementation note: paths where the floor never runs at
# all (no candidates, buy_floor unset, absolute floor mode, sim bypass)
# record "deconfigured" unless the guard is validly configured, the scan is
# small, and the partition is NOT CLEAN — that case records the suppression
# so a generation-starved empty/absent scan still leaves the loud record
# (AC-F's limiting shape).
BRANCH_ACTED = "acted"
BRANCH_NOT_SMALL_N = "not_small_n"
BRANCH_DECONFIGURED = "deconfigured"


def emit_expected_universe(ctx: Any, universe: Any) -> None:
    """Record the candidate-generation universe BEFORE any drop (§2 cond 1).

    Called by the candidate-generation stage (``pp_inference`` Phase 2b)
    with the exact ticker list handed to ``TickerCandidateJob`` — watchlist
    ∩ session eligibility. Emits BOTH the counter (mass-balance arithmetic)
    and the ticker list (set accounting + POLICY set identity).
    """
    tickers = sorted({str(t) for t in (universe or [])})
    counters = getattr(ctx, "counters", None)
    if isinstance(counters, dict):
        counters[EXPECTED_UNIVERSE_COUNTER] = len(tickers)
    try:
        setattr(ctx, EXPECTED_UNIVERSE_TICKERS_ATTR, tickers)
    except AttributeError:  # frozen/slotted test contexts: counter suffices
        pass


def eligibility_config(panel_cfg: dict) -> dict:
    """Resolve the frozen allowlist config with spec defaults.

    ``ranking.panel_scoring.smalln_eligibility``:

    - ``integrity_share_bounds``: {class: max share of expected_universe}
      — merged over :data:`DEFAULT_INTEGRITY_SHARE_BOUNDS`; invalid values
      (non-finite, outside [0, 1]) are ignored with an ERROR (the default
      bound stays — misconfig must never widen a bound silently).
    - ``policy_reasons``: {exact reason string: {"eligible": [tickers]}} —
      POLICY-classed narrowing declarations. No defaults: POLICY reasons
      exist only when reviewed, pinned config declares them.
    - ``integrity_reasons``: {exact reason string: class} — optional
      config-frozen EXTENSIONS to the built-in reason→class map (a new
      approved-normal surface must be allowlisted here to stop failing
      CLEAN as unknown).
    """
    raw = panel_cfg.get(CONFIG_KEY) or {}
    if not isinstance(raw, dict):
        log.error(
            "smalln_eligibility config REJECTED — not a dict (%r); "
            "spec defaults apply", type(raw).__name__,
        )
        raw = {}
    bounds = dict(DEFAULT_INTEGRITY_SHARE_BOUNDS)
    raw_bounds = raw.get("integrity_share_bounds") or {}
    if isinstance(raw_bounds, dict):
        for cls, val in raw_bounds.items():
            ok = (
                isinstance(val, (int, float))
                and not isinstance(val, bool)
                and math.isfinite(float(val))
                and 0.0 <= float(val) <= 1.0
            )
            if ok:
                bounds[str(cls)] = float(val)
            else:
                log.error(
                    "smalln_eligibility.integrity_share_bounds[%r]=%r "
                    "invalid (must be finite in [0, 1]); default bound "
                    "stays", cls, val,
                )
    policy = raw.get("policy_reasons") or {}
    if not isinstance(policy, dict):
        log.error(
            "smalln_eligibility.policy_reasons invalid (%r); ignored",
            type(policy).__name__,
        )
        policy = {}
    extra_integrity = raw.get("integrity_reasons") or {}
    if not isinstance(extra_integrity, dict):
        log.error(
            "smalln_eligibility.integrity_reasons invalid (%r); ignored",
            type(extra_integrity).__name__,
        )
        extra_integrity = {}
    return {
        "integrity_share_bounds": bounds,
        "policy_reasons": {str(k): v for k, v in policy.items()},
        "integrity_reasons": {
            str(k): str(v) for k, v in extra_integrity.items()
        },
    }


def _classify_integrity(reason: str, extra: dict[str, str]) -> str | None:
    if reason in extra:
        return extra[reason]
    if reason in _INTEGRITY_REASON_EXACT:
        return _INTEGRITY_REASON_EXACT[reason]
    for prefix, cls in _INTEGRITY_REASON_PREFIX.items():
        if reason.startswith(prefix):
            return cls
    return None


def _int_counter(counters: Any, key: str) -> int | None:
    if not isinstance(counters, dict) or key not in counters:
        return None
    val = counters.get(key)
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        return None
    return val


def build_partition(
    *,
    watchlist: list[str],
    counters: Any,
    universe_tickers: list[str] | None,
    survivor_tickers: list[str],
    finite_n: int,
    nonfinite: int,
    scored: int,
    blocked: dict[str, str],
    markers: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the §2 partition record from already-extracted surfaces.

    Caller-agnostic: the kernel task and the ``panel_scoring`` twin each
    extract their own surfaces and share this partition + CLEAN logic (one
    implementation — the twin lockstep applies to the FLOOR helpers, the
    eligibility logic is deliberately single-source).
    """
    expected = _int_counter(counters, EXPECTED_UNIVERSE_COUNTER)
    survivors = {str(t) for t in survivor_tickers}
    universe = (
        [str(t) for t in universe_tickers]
        if universe_tickers is not None else None
    )
    score_missing = _int_counter(counters, "panel_score_missing") or 0

    pre_floor: dict[str, int] = {}
    unaccounted: list[str] = []
    if universe is not None:
        uni_set = set(universe)
        for t in universe:
            if t in survivors:
                continue
            reason = blocked.get(t)
            if reason is None:
                unaccounted.append(t)
            elif reason not in _SCAN_DROP_REASONS:
                pre_floor[reason] = pre_floor.get(reason, 0) + 1
        surplus = sorted(survivors - uni_set)
    else:
        # Counter-only emitters (no ticker list): count-based fallback.
        # Blocked entries for names outside the buy universe (holdings
        # tagged exit-only, missing_ohlcv names) cannot be distinguished,
        # so the arithmetic check below is best-effort and the set-identity
        # assertion falls back to the full blocked map.
        for t, reason in blocked.items():
            if t in survivors or reason in _SCAN_DROP_REASONS:
                continue
            pre_floor[reason] = pre_floor.get(reason, 0) + 1
        surplus = []

    entered_scan = len(survivors) + score_missing
    return {
        "schema_version": SMALLN_LEDGER_SCHEMA_VERSION,
        # (b) config-frozen watchlist outer anchor (approving review).
        "watchlist_size": len(watchlist),
        "expected_universe": expected,
        "expected_universe_tickers_recorded": universe is not None,
        "entered_scan": entered_scan,
        "scored": scored,
        "score_missing": score_missing,
        "nonfinite": nonfinite,
        "finite_n": finite_n,
        "pre_floor_exclusions": dict(sorted(pre_floor.items())),
        "unaccounted": sorted(unaccounted),
        "scan_surplus": surplus,
        "failure_markers": {k: v for k, v in markers.items() if v},
    }


def evaluate_clean(
    partition: dict[str, Any],
    *,
    watchlist: list[str],
    blocked: dict[str, str],
    config: dict[str, Any],
) -> tuple[bool, str | None]:
    """The §2 CLEAN predicate. Returns (clean, first-failing-class reason).

    Check order follows the amendment's numbering; the recorded suppression
    reason is the FIRST failing class.
    """
    # 1. Mass balance (fail-closed on the missing record, not just the
    #    recorded-unknown one — AC-D).
    expected = partition.get("expected_universe")
    if expected is None:
        return False, "mass_balance:expected_universe_absent"
    if partition.get("expected_universe_tickers_recorded"):
        n_unaccounted = len(partition.get("unaccounted") or [])
        if n_unaccounted:
            return False, f"mass_balance:unaccounted={n_unaccounted}"
        if partition.get("scan_surplus"):
            return (
                False,
                f"mass_balance:scan_surplus={len(partition['scan_surplus'])}",
            )
        # Counter cross-check (§2 condition 1 arithmetic): entered_scan +
        # Σ(recorded pre-scan exclusion counts) == expected_universe. With
        # the set accounting above this can only fire on a counter/list
        # inconsistency (e.g. a scan-drop tag without its counter) —
        # instrumentation disagreement is NOT CLEAN, fail-closed.
        recorded_universe = partition["entered_scan"] + sum(
            (partition.get("pre_floor_exclusions") or {}).values()
        )
        if recorded_universe != expected:
            return (
                False,
                f"mass_balance:counter_mismatch expected={expected} "
                f"accounted={recorded_universe}",
            )
    else:
        accounted = partition["entered_scan"] + sum(
            (partition.get("pre_floor_exclusions") or {}).values()
        )
        if accounted != expected:
            return (
                False,
                f"mass_balance:accounted={accounted} != "
                f"expected={expected}",
            )

    # 2. Funnel integrity.
    if partition.get("score_missing"):
        return (
            False,
            f"funnel_integrity:panel_score_missing="
            f"{partition['score_missing']}",
        )
    if partition.get("nonfinite"):
        return (
            False,
            f"funnel_integrity:rank_score_nan={partition['nonfinite']}",
        )

    # 3. Allowlist membership + INTEGRITY share bounds + POLICY set identity.
    #
    # POLICY narrowing is applied BEFORE the generation stage (the emitted
    # universe is already watchlist ∩ session eligibility), so its tags
    # live on names OUTSIDE the universe. Every DECLARED policy reason is
    # therefore asserted unconditionally against the FULL blocked map —
    # a declaration in reviewed, pinned config means the narrowing is
    # supposed to be in force; an untagged or wrongly-tagged application
    # of it is exactly the record-full masquerade the approving review's
    # expectation (a) closes.
    bounds = config["integrity_share_bounds"]
    policy_reasons = config["policy_reasons"]
    extra_integrity = config["integrity_reasons"]
    for reason in sorted(policy_reasons):
        failure = _policy_set_identity_failure(
            reason,
            declaration=policy_reasons[reason],
            watchlist=watchlist,
            blocked=blocked,
        )
        if failure:
            return False, failure
    class_counts: dict[str, int] = {}
    for reason, count in (partition.get("pre_floor_exclusions") or {}).items():
        if reason in policy_reasons:
            continue  # exact declared string; identity asserted above
        cls = _classify_integrity(reason, extra_integrity)
        if cls is None:
            return False, f"unknown_exclusion_reason:{reason}"
        class_counts[cls] = class_counts.get(cls, 0) + count
    if expected > 0:
        for cls, count in sorted(class_counts.items()):
            bound = bounds.get(cls)
            if bound is None:
                return False, f"unknown_exclusion_reason:class:{cls}"
            share = count / expected
            if share > bound:
                return (
                    False,
                    f"share_bound:{cls} share={share:.3f} > "
                    f"bound={bound:.2f}",
                )

    # 4. Failure markers.
    markers = partition.get("failure_markers") or {}
    if markers:
        first = sorted(markers)[0]
        return False, f"failure_marker:{first}"

    return True, None


def _policy_set_identity_failure(
    reason: str,
    *,
    declaration: Any,
    watchlist: list[str],
    blocked: dict[str, str],
) -> str | None:
    """Implementation expectation (a): POLICY-tagged set == watchlist −
    config-declared eligible set, else NOT CLEAN.

    The comparison uses the FULL blocked map (POLICY narrowing is declared
    against the watchlist, before universe carving), and the reason string
    must match the declared key EXACTLY (dict-key lookup upstream enforces
    that). A malformed declaration fails closed.
    """
    if not isinstance(declaration, dict) or not isinstance(
        declaration.get("eligible"), list
    ):
        return f"policy_set_identity:{reason}:malformed_declaration"
    declared_eligible = {str(t) for t in declaration["eligible"]}
    expected_excluded = {str(t) for t in watchlist} - declared_eligible
    actual_tagged = {t for t, r in blocked.items() if r == reason}
    if actual_tagged != expected_excluded:
        missing = len(expected_excluded - actual_tagged)
        extra = len(actual_tagged - expected_excluded)
        return (
            f"policy_set_identity:{reason}:missing={missing},"
            f"unexpected={extra}"
        )
    return None


def collect_failure_markers(ctx: Any, *, nonfinite: int) -> dict[str, Any]:
    """§2 condition 4 marker sweep over the v1 detectable surfaces."""
    counters = getattr(ctx, "counters", None)
    markers: dict[str, Any] = {
        "panel_scoring_contract_failed": bool(
            getattr(ctx, "_panel_scoring_contract_failed", False)
        ),
        "calibrator_contract_failed": bool(
            getattr(ctx, "_calibrator_contract_failed", False)
        ),
        "feed_staleness_flagged": bool(
            getattr(ctx, "_feed_staleness_flagged", False)
        ),
        "panel_score_missing": bool(
            _int_counter(counters, "panel_score_missing")
        ),
        "rank_score_nan": bool(nonfinite),
    }
    return markers


def kernel_partition(ctx: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Extract kernel-funnel surfaces and build the partition (pure reads)."""
    config = getattr(ctx, "config", None) or {}
    watchlist = [str(t) for t in (config.get("watchlist") or [])]
    blocked = dict(getattr(ctx, "_blocked_by_ticker", None) or {})
    candidates = list(getattr(ctx, "candidates", None) or [])
    survivor_tickers: list[str] = []
    finite_n = 0
    nonfinite = 0
    scored = 0
    for cand in candidates:
        ticker = getattr(cand, "ticker", None)
        if ticker:
            survivor_tickers.append(str(ticker))
        score = getattr(cand, "rank_score", None)
        if score is None:
            continue
        try:
            f = float(score)
        except (TypeError, ValueError):
            nonfinite += 1
            scored += 1
            continue
        scored += 1
        if math.isfinite(f):
            finite_n += 1
        else:
            nonfinite += 1
    universe_tickers = getattr(ctx, EXPECTED_UNIVERSE_TICKERS_ATTR, None)
    if not isinstance(universe_tickers, list):
        universe_tickers = None
    partition = build_partition(
        watchlist=watchlist,
        counters=getattr(ctx, "counters", None),
        universe_tickers=universe_tickers,
        survivor_tickers=survivor_tickers,
        finite_n=finite_n,
        nonfinite=nonfinite,
        scored=scored,
        blocked=blocked,
        markers=collect_failure_markers(ctx, nonfinite=nonfinite),
    )
    return partition, blocked


def twin_partition(ctx: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Extract ``renquant_pipeline.panel_scoring`` twin surfaces.

    The twin's simplified contract has no candidate objects: the scan set
    is the watchlist scored through ``ctx.panel_scores``; exclusions live
    on ``ctx.blocked_by``. The generation counter is normally ABSENT there
    → NOT CLEAN → the twin's small-n branch suppresses (fail-closed on
    missing records, amendment AC-D) unless the driving harness emits the
    counter.
    """
    cfg = getattr(ctx, "strategy_config", {}) or {}
    watchlist = [str(t) for t in (cfg.get("watchlist") or [])]
    blocked = dict(getattr(ctx, "blocked_by", None) or {})
    scores_map = getattr(ctx, "panel_scores", {}) or {}
    survivor_tickers: list[str] = []
    finite_n = 0
    nonfinite = 0
    scored = 0
    for ticker in watchlist:
        if ticker not in scores_map:
            continue
        survivor_tickers.append(ticker)
        value = scores_map.get(ticker)
        if value is None:
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            nonfinite += 1
            scored += 1
            continue
        scored += 1
        if math.isfinite(f):
            finite_n += 1
        else:
            nonfinite += 1
    universe_tickers = getattr(ctx, EXPECTED_UNIVERSE_TICKERS_ATTR, None)
    if not isinstance(universe_tickers, list):
        universe_tickers = None
    partition = build_partition(
        watchlist=watchlist,
        counters=getattr(ctx, "counters", None),
        universe_tickers=universe_tickers,
        survivor_tickers=survivor_tickers,
        finite_n=finite_n,
        nonfinite=nonfinite,
        scored=scored,
        blocked=blocked,
        markers=collect_failure_markers(ctx, nonfinite=nonfinite),
    )
    return partition, blocked


def log_suppression(reason: str, *, n_finite: int, min_n: int) -> None:
    """The LOUD tag — exactly one grep-able ERROR line per suppression."""
    log.error(
        "VetoWeakBuysTask: %s(reason=%s) — partition NOT CLEAN at "
        "finite n=%d < N0=%d; relax-only branch MUST NOT act, status-quo "
        "floor stands (fails toward no-entry). A suppression on a small-n "
        "day is exactly a day a human should look at (amendment §2).",
        SUPPRESSION_TAG, reason, n_finite, min_n,
    )


def finalize_ledger_block(
    ctx: Any,
    *,
    partition: dict[str, Any],
    clean: bool,
    clean_reason: str | None,
    branch_action: str,
    n0: int | None,
    original_floor: float | None,
    relaxed_floor: float | None,
    candidate_delta: list[str],
    submit_registry: bool = True,
) -> dict[str, Any]:
    """Attach the §3 schema-versioned block to ctx and the gate registry."""
    suppressed_reason = (
        branch_action.split(":", 1)[1]
        if branch_action.startswith("suppressed:") else None
    )
    block = {
        **partition,
        "clean": clean,
        "not_clean_reason": clean_reason,
        "n0": n0,
        "original_floor": original_floor,
        "relaxed_floor": relaxed_floor,
        "branch_action": branch_action,
        "suppressed_reason": suppressed_reason,
        "candidate_delta": list(candidate_delta),
    }
    try:
        setattr(ctx, LEDGER_ATTR, block)
    except AttributeError:
        pass
    if submit_registry:
        try:
            from renquant_pipeline.kernel.gate_registry import (  # noqa: PLC0415
                ctx_registry,
            )
            ctx_registry(ctx).submit(
                gate="smalln_eligibility",
                scope="book",
                verdict="allow",  # observability row; lattice-neutral
                reason=branch_action,
                inputs=block,
            )
        except Exception:  # noqa: BLE001 — persistence must not kill the scan
            log.exception("smalln_eligibility: gate-registry submit failed")
    return block


__all__ = [
    "SMALLN_LEDGER_SCHEMA_VERSION",
    "EXPECTED_UNIVERSE_COUNTER",
    "EXPECTED_UNIVERSE_TICKERS_ATTR",
    "SUPPRESSION_TAG",
    "LEDGER_ATTR",
    "CONFIG_KEY",
    "DEFAULT_INTEGRITY_SHARE_BOUNDS",
    "BRANCH_ACTED",
    "BRANCH_NOT_SMALL_N",
    "BRANCH_DECONFIGURED",
    "emit_expected_universe",
    "eligibility_config",
    "build_partition",
    "evaluate_clean",
    "collect_failure_markers",
    "kernel_partition",
    "twin_partition",
    "log_suppression",
    "finalize_ledger_block",
]
