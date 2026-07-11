"""ParkingSleeveShadowTask — S7 lane-B parking sleeve (β-budgeted SPY/SGOV).

Class name retained from the original shadow-only change (#157) for wiring
and audit continuity; since this change the task carries BOTH modes
(``shadow`` default, ``live`` = SGOV-floor order emission — see below).

Design contract (both merged on renquant-orchestrator main):
  * doc/research/2026-07-02-rs1-parking-sleeve.md (RS-1, r2)
  * doc/design/2026-07-02-104-capability-program.md §1.3 (P1-2)

What this task does
-------------------
Idle cash above a reserve (``reserve_pv_pct``·PV + open-order headroom +
the regime ``cash_reserve_pct`` fraction of PV) is swept into a two-leg
parking sleeve:

  sleeve_spy_frac = max(0, (beta_max − w_pos·beta_pos) / w_sleeve)   [capped at 1]

SPY leg + SGOV remainder (RS-1 §2 — the split is PROVISIONAL, ``beta_pos = 1.0``
is a conservative assumption, not a measurement). The sleeve sells FIRST to
fund admitted single-name buys, and the regime reserve gates apply: BEAR
(``cash_reserve_pct = 1``) sweeps the sleeve fully off; CHOPPY/BULL_VOLATILE
reserves scale it down.

Rollout state (THIS module)
---------------------------
* **Default OFF.** The task is a no-op unless ``config["sleeve"]["enabled"]``
  is truthy — with the flag absent it reads nothing else, writes nothing,
  and mutates no ctx field (pinned by tests/test_parking_sleeve.py).
* **Shadow default.** In ``sleeve.mode = "shadow"`` the task computes the
  intended sweep/fund orders and appends them to a dedicated JSONL under
  ``logs/`` — schema ``{date, action, symbol, qty, notional, reason,
  book_state}`` — placing NOTHING (never touches ``ctx.orders`` /
  ``ctx.exits``; runtime-asserted). Shadow behavior is byte-identical to
  the original #157 implementation (pinned by regression tests): it still
  models the RS-1 β-budgeted SPY/SGOV split and does NOT apply the
  ``max_sleeve_pct`` cap, so the shadow corpus feeding the RS-1 §4
  pre-registered comparison is not silently changed mid-collection.
* **Live = SGOV floor only.** ``sleeve.mode = "live"`` emits REAL order
  intents, restricted to the RS-1 §2/§4 "floor variant": SGOV buys of idle
  cash above the reserves, SGOV sells whenever cash is needed (sells always
  win — free-before-need). The SPY arm stays DARK in live mode by
  construction (never a SPY order, buy or sell): RS-1 §4 requires the SPY
  arm's own pre-registered comparison + a recorded capital authorization,
  mirroring strategy-104's ``spy_arm_gate_cleared`` structural guard
  (strategy-104 #44). Enabling live SPY exposure is a separate, gated
  change — there is deliberately no config knob for it here.
  Live-mode invariants:
    - liquidity: a sleeve buy only uses cash above
      ``reserve_pv_pct``·PV + pending admitted buys + the regime reserve,
      so the sleeve can never starve a main-strategy buy; when cash falls
      short of those reserves the sleeve SELLS first (exits are executed
      before buys — pp_execution's ExitsJob→BuysJob ordering invariant).
    - ``max_sleeve_pct`` (default 0.50, strategy-104 #44 semantics) caps
      CUMULATIVE cross-session sleeve exposure against the REAL broker
      SGOV holding, and additionally rebalances an over-cap sleeve back
      down (subject to the dust threshold).
    - fail-closed on missing SGOV price: no buy is ever emitted without a
      positive SGOV price (the umbrella daily price fetch does not cover
      SGOV yet — umbrella/base-data follow-up); if cash is needed while
      the price is missing, the FULL position is liquidated (a full exit
      needs no price) so the liquidity invariant still holds.
    - SGOV buys pass through the existing cost-aware §1091 wash-sale
      engine (``is_wash_sale_blocked_with_cost``): a recent SGOV loss
      sale blocks the re-buy; ETF-at-gain sales pass. Sells are never
      wash-sale blocked.
    - live sleeve buys respect the book-level buy gates
      (``buy_blocked`` / ``skip_buys`` / ``bear_only``); sells ignore
      them (exits-always-allowed).
* The shadow sleeve book persists across sessions inside the JSONL itself
  (the last ``record_type == "summary"`` row carries ``shadow_state``), so
  the 10-session shadow AC exercises the incremental sweep / sell-to-fund
  round-trip, not a fresh full sweep every day. Live mode does NOT use the
  JSONL book — the broker holdings are the cross-session truth; the JSONL
  keeps being written (same schema, ``book_state.mode = "live"``) for
  monitoring and the summary row's ``shadow_state`` mirrors the REAL
  post-trade book so a later mode flip never inherits a stale shadow book.

Monitoring rule (RS-1 §4/§5) — metric emission only
---------------------------------------------------
The reversal trigger (3-month negative sleeve contribution AND >50%
DD-budget consumption ⇒ drop to the SGOV floor) is a MONITORING rule. This
task emits the inputs — ``sleeve_contribution_abs/pct``,
``dd_budget_consumption_pct`` and its running max — in every record; it
does not trade on them.

Risk-control participation (RS-1 §3)
------------------------------------
Per the merged RS-1 r2 correction, the SPY leg is a REAL beta position, not
cash-equivalent: when the live path is built it must participate in book
beta / gross-net / concentration / drawdown accounting, excluded ONLY from
single-name alpha ranking and the panel-exit's rotation logic. Nothing in
shadow mode creates positions, so that participation is a live-enablement
follow-up, out of scope here by construction.

Simplifications (stated, per RS-1 §2):
  * The SGOV leg is tracked at COST (T-bill ETF ≈ flat NAV; carry is not
    modeled — conservative: shadow contribution understates the SGOV arm).
  * SPY whole shares only (fractional shares operator-closed 2026-06-30);
    rounding dust stays in cash.
  * The regime ``cash_reserve_pct`` is used RAW (no confidence multiplier,
    unlike buy-side sizing) so BEAR = 1.0 always fully disables the sleeve.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX-only; Windows falls back to no cross-process lock
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

from .context import InferenceContext
from .order_attribution import stamp_order_attribution
from .pipeline import Task
from .task_benchmark_sleeve import (
    _buy_cost_multiplier,
    _existing_buy_tickers,
    _existing_exit_tickers,
    _finite_float,
    _pending_buy_invest,
    _sell_proceeds_multiplier,
)

log = logging.getLogger("kernel.pipeline.parking_sleeve")

DEFAULT_LOG_PATH = "logs/parking_sleeve_shadow.jsonl"

# Live mode caps CUMULATIVE cross-session sleeve exposure at
# max_sleeve_pct · PV against the REAL broker SGOV holding — the same
# semantics strategy-104 #44 pinned for its ParkingSleeveConfig (default
# 0.50). Shadow mode deliberately does NOT cap (byte-identical #157
# behavior; see module docstring).
DEFAULT_MAX_SLEEVE_PCT_LIVE = 0.50

# SGOV is tracked at COST, not marked to market: the shadow book only moves
# the SGOV leg's value via BUY/SELL notional (see module docstring — T-bill
# carry/accretion is not modeled). Stamped into every book_state so a
# consumer of the raw JSONL cannot misread ``sleeve_contribution_pct`` as
# capturing SGOV's real economics; the SPY leg IS marked to market by
# contrast (``shadow_spy_qty * spy_price``), so the two legs are NOT
# treated symmetrically and this makes that asymmetry explicit rather than
# implicit in the arithmetic.
SGOV_VALUATION_MODE = "cost_no_carry"

# Live mode marks the REAL broker SGOV holding to market (entry_price cost
# basis vs current price) — the opposite convention from the shadow book.
# Stamped so mixed shadow/live logs are self-describing; the operational
# scorecard's sgov_valuation_mode_consistent flag flipping to False after a
# mode change is correct and intentional signal, not noise.
SGOV_VALUATION_MODE_LIVE = "mark_to_market"

# ── Operational vs economic field split (see build_operational_scorecard /
# build_economic_scorecard below) — a clean operational log (idempotent,
# schema-complete, no errors) is NOT evidence the sleeve strategy itself is
# a good idea; the two questions must never be blended into one scorecard.
OPERATIONAL_BOOK_STATE_FIELDS = frozenset({
    "mode", "live_orders_placed", "blocked", "sgov_valuation_mode",
})
ECONOMIC_BOOK_STATE_FIELDS = frozenset({
    "pv", "shadow_pv", "cash", "shadow_cash", "positions_value",
    "pending_buy_notional", "regime", "regime_cash_reserve_pct",
    "reserve_operational", "reserve_regime", "deployable", "funding_shortfall",
    "w_pos", "w_sleeve", "sleeve_spy_frac", "target_spy_value",
    "target_sgov_value", "spy_price", "sgov_price", "shadow_spy_qty",
    "shadow_sgov_value", "net_invested", "sleeve_value",
    "sleeve_contribution_abs", "sleeve_contribution_pct", "drawdown_pct",
    "dd_budget_pct", "dd_budget_consumption_pct",
    "max_dd_budget_consumption_pct",
})

_EMPTY_SHADOW_STATE: dict[str, float] = {
    "spy_qty": 0.0,
    "sgov_value": 0.0,          # at cost — see module docstring
    "net_invested": 0.0,        # cum buys − cum sell proceeds
    "last_spy_price": 0.0,
    "max_dd_budget_consumption_pct": 0.0,
}


def parking_sleeve_config(obj: Any) -> dict:
    cfg = obj if isinstance(obj, dict) else (getattr(obj, "config", None) or {})
    if not isinstance(cfg, dict):
        return {}
    sleeve = cfg.get("sleeve")
    return sleeve if isinstance(sleeve, dict) else {}


def is_parking_sleeve_enabled(obj: Any) -> bool:
    return bool(parking_sleeve_config(obj).get("enabled", False))


def compute_parking_sleeve_plan(
    *,
    pv: float,
    cash: float,
    positions_value: float,
    spy_qty: float,
    spy_price: float | None,
    sgov_value: float,
    sgov_price: float | None,
    pending_buy_notional: float = 0.0,
    regime_cash_reserve_pct: float = 0.0,
    reserve_pv_pct: float = 0.05,
    beta_max: float = 0.6,
    beta_pos: float = 1.0,
    min_trade_notional: float = 50.0,
    max_sleeve_pct: float = 1.0,
    sgov_only: bool = False,
    spy_symbol: str = "SPY",
    sgov_symbol: str = "SGOV",
) -> dict[str, Any]:
    """Pure β-budgeted sweep/fund planner (no ctx, no I/O — unit-testable).

    Returns a dict with the intended ``actions`` (sells FIRST, then buys)
    and every intermediate quantity needed for the shadow log's
    ``book_state``. Monetary inputs are dollars; ``sgov_value`` is the
    current SGOV leg at cost.

    ``max_sleeve_pct`` caps the TARGET total sleeve at that fraction of PV
    (strategy-104 #44 cumulative cross-session semantics: the current leg
    values count against the cap, and an over-cap sleeve rebalances back
    down). The default 1.0 never binds — shadow callers stay byte-identical
    to #157; the live path passes the configured cap.

    ``sgov_only=True`` forces ``sleeve_spy_frac = 0`` (the RS-1 §2 SGOV
    floor variant) — the live path uses this so the un-authorized SPY arm
    can never receive capital.
    """
    pv = _finite_float(pv, 0.0)
    if pv <= 0:
        return {"actions": [], "reason": "invalid_pv", "blocked": ["invalid_pv"]}

    cash = _finite_float(cash, 0.0)
    positions_value = max(_finite_float(positions_value, 0.0), 0.0)
    spy_qty = max(_finite_float(spy_qty, 0.0), 0.0)
    spy_price = _finite_float(spy_price, 0.0)
    sgov_value = max(_finite_float(sgov_value, 0.0), 0.0)
    sgov_price = _finite_float(sgov_price, 0.0)
    pending_buy_notional = max(_finite_float(pending_buy_notional, 0.0), 0.0)
    rr = min(max(_finite_float(regime_cash_reserve_pct, 0.0), 0.0), 1.0)
    reserve_pv_pct = min(max(_finite_float(reserve_pv_pct, 0.05), 0.0), 1.0)
    beta_max = max(_finite_float(beta_max, 0.6), 0.0)
    beta_pos = max(_finite_float(beta_pos, 1.0), 0.0)
    min_trade = max(_finite_float(min_trade_notional, 50.0), 0.0)
    max_sleeve_pct = min(max(_finite_float(max_sleeve_pct, 1.0), 0.0), 1.0)

    spy_value = spy_qty * spy_price if spy_price > 0 else 0.0
    sleeve_value = spy_value + sgov_value

    reserve_operational = reserve_pv_pct * pv + pending_buy_notional
    reserve_regime = rr * pv
    deployable = max(0.0, cash + sleeve_value - reserve_operational - reserve_regime)
    funding_shortfall = max(0.0, reserve_operational + reserve_regime - cash)

    # strategy-104 #44 cumulative cap: the TARGET total sleeve never exceeds
    # max_sleeve_pct·PV. Because ``deployable`` is the target and the current
    # legs count inside it, this enforces the cap across sessions and also
    # rebalances an over-cap sleeve back down (dust threshold still applies).
    max_sleeve_value = max_sleeve_pct * pv
    cap_bound = deployable > max_sleeve_value
    deployable = min(deployable, max_sleeve_value)

    w_pos = positions_value / pv
    w_sleeve = deployable / pv
    if sgov_only or w_sleeve <= 0.0:
        sleeve_spy_frac = 0.0
    else:
        sleeve_spy_frac = min(max((beta_max - w_pos * beta_pos) / w_sleeve, 0.0), 1.0)
    target_spy_value = deployable * sleeve_spy_frac
    target_sgov_value = deployable - target_spy_value

    bear_off = rr >= 1.0
    if bear_off:
        reason = "bear_regime_sleeve_off"
    elif funding_shortfall > 0.0 and pending_buy_notional > 0.0:
        reason = "sell_first_fund_admitted_buys"
    elif funding_shortfall > 0.0:
        reason = "reserve_shortfall_scale_down"
    elif rr > 0.0:
        reason = "regime_reserve_scaled_sweep"
    elif cap_bound:
        reason = "max_sleeve_cap_enforced"
    elif sleeve_value <= 0.0:
        reason = "sweep_idle_cash"
    else:
        reason = "rebalance_beta_budget"
    # Sells that keep buys funded / honor a defensive regime bypass the
    # dust threshold; pure rebalance churn does not.
    force_sells = bear_off or funding_shortfall > 0.0

    blocked: list[str] = []
    sells: list[dict[str, Any]] = []
    buys: list[dict[str, Any]] = []

    delta_spy = target_spy_value - spy_value
    delta_sgov = target_sgov_value - sgov_value

    # ── sells FIRST (SGOV — the cash-like leg — before SPY) ──────────────
    if delta_sgov < 0 and (force_sells or -delta_sgov >= min_trade):
        notional = min(sgov_value, -delta_sgov)
        if notional > 0:
            qty = math.ceil(notional / sgov_price) if sgov_price > 0 else None
            sells.append({
                "action": "SELL", "symbol": sgov_symbol,
                "qty": qty, "notional": round(notional, 2), "reason": reason,
            })
    if delta_spy < 0 and (force_sells or -delta_spy >= min_trade):
        if spy_price > 0:
            qty = min(int(spy_qty), int(math.ceil(-delta_spy / spy_price)))
            if qty >= 1:
                sells.append({
                    "action": "SELL", "symbol": spy_symbol,
                    "qty": qty, "notional": round(qty * spy_price, 2),
                    "reason": reason,
                })
        elif spy_qty > 0:
            blocked.append("spy_price_missing_for_sell")

    # ── buys (SPY leg, then SGOV remainder) ──────────────────────────────
    if delta_spy > 0 and delta_spy >= min_trade:
        if spy_price > 0:
            qty = int(delta_spy // spy_price)
            if qty >= 1:
                buys.append({
                    "action": "BUY", "symbol": spy_symbol,
                    "qty": qty, "notional": round(qty * spy_price, 2),
                    "reason": reason,
                })
        else:
            blocked.append("spy_price_missing_for_buy")
    if delta_sgov > 0 and delta_sgov >= min_trade:
        if sgov_price > 0:
            qty = int(delta_sgov // sgov_price)
            notional = qty * sgov_price
        else:
            # SGOV tracked at cost; a missing price does not block the
            # sweep computation (qty unknown until the data plane adds it).
            qty = None
            notional = delta_sgov
        if notional > 0 and (qty is None or qty >= 1):
            buys.append({
                "action": "BUY", "symbol": sgov_symbol,
                "qty": qty, "notional": round(notional, 2), "reason": reason,
            })

    return {
        "actions": sells + buys,
        "reason": reason,
        "blocked": blocked,
        "reserve_operational": reserve_operational,
        "reserve_regime": reserve_regime,
        "deployable": deployable,
        "funding_shortfall": funding_shortfall,
        "w_pos": w_pos,
        "w_sleeve": w_sleeve,
        "sleeve_spy_frac": sleeve_spy_frac,
        "target_spy_value": target_spy_value,
        "target_sgov_value": target_sgov_value,
        "sleeve_value": sleeve_value,
        "spy_value": spy_value,
        "sgov_value": sgov_value,
        "max_sleeve_pct": max_sleeve_pct,
        "max_sleeve_value_cap": max_sleeve_value,
        "sleeve_cap_bound": cap_bound,
        "sgov_only": bool(sgov_only),
    }


def load_last_shadow_state(path: str | Path) -> dict[str, float]:
    """Return the shadow sleeve book persisted in the JSONL's last summary row."""
    out = dict(_EMPTY_SHADOW_STATE)
    p = Path(path)
    if not p.exists():
        return out
    last: dict | None = None
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("record_type") == "summary":
                    state = row.get("shadow_state")
                    if isinstance(state, dict):
                        last = state
    except OSError:
        log.warning("parking sleeve: unreadable shadow log %s — starting fresh", p)
        return out
    if last:
        for key in out:
            out[key] = _finite_float(last.get(key), out[key])
    return out


def _has_logged_date(path: str | Path, date_str: str | None) -> bool:
    """True iff a ``summary`` row for ``date_str`` already exists in the log.

    Idempotency guard: without this, re-running the task for a date it has
    already processed (a retry after a transient failure, or two runs
    racing on the same host) would append a second set of action/summary
    rows and roll the shadow book forward a second time for the same
    calendar date, silently double-applying that day's intended sweep.
    """
    if date_str is None:
        return False
    p = Path(path)
    if not p.exists():
        return False
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(row, dict)
                    and row.get("record_type") == "summary"
                    and row.get("date") == date_str
                ):
                    return True
    except OSError:
        return False
    return False


def build_operational_scorecard(rows: list[dict]) -> dict[str, Any]:
    """Instrumentation hygiene only — does the shadow logger itself work?

    Never evidence that the parking-sleeve STRATEGY is a good idea; see
    ``build_economic_scorecard`` for that separate question. A clean
    operational scorecard must never be presented or interpreted as
    authorization evidence for live capital deployment.
    """
    summaries = [r for r in rows if isinstance(r, dict) and r.get("record_type") == "summary"]
    required = {"date", "action", "symbol", "qty", "notional", "reason", "book_state"}
    schema_complete = all(required <= set(r) for r in rows if isinstance(r, dict))
    dates = [r.get("date") for r in summaries]
    duplicate_dates = len(dates) - len(set(dates))
    blocked_counts: dict[str, int] = {}
    sgov_modes: set[str] = set()
    for r in summaries:
        bs = r.get("book_state") or {}
        for reason in bs.get("blocked") or []:
            blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
        mode = bs.get("sgov_valuation_mode")
        if mode:
            sgov_modes.add(mode)
    return {
        "n_summary_rows": len(summaries),
        "schema_complete": schema_complete,
        "duplicate_summary_dates": duplicate_dates,
        "blocked_reason_counts": blocked_counts,
        "sgov_valuation_modes_seen": sorted(sgov_modes),
        "sgov_valuation_mode_consistent": len(sgov_modes) <= 1,
    }


def build_economic_scorecard(rows: list[dict]) -> dict[str, Any]:
    """Shadow-simulated economic merit — separate from operational hygiene.

    ``authorization_grade`` is always ``False``: this is shadow-only
    (no live capital at risk), subject to the same look-ahead / regime-
    coverage caveats as any other shadow-only measurement, and is at most
    the beginning of an eventual economic case — never sufficient alone to
    authorize live deployment.
    """
    summaries = [r for r in rows if isinstance(r, dict) and r.get("record_type") == "summary"]
    if not summaries:
        return {"authorization_grade": False, "n_sessions": 0}
    last_bs = summaries[-1].get("book_state") or {}
    contributions = [
        _finite_float((r.get("book_state") or {}).get("sleeve_contribution_pct"), 0.0)
        for r in summaries
    ]
    return {
        "authorization_grade": False,
        "n_sessions": len(summaries),
        "final_sleeve_contribution_abs": last_bs.get("sleeve_contribution_abs"),
        "final_sleeve_contribution_pct": last_bs.get("sleeve_contribution_pct"),
        "final_max_dd_budget_consumption_pct": last_bs.get("max_dd_budget_consumption_pct"),
        "mean_sleeve_contribution_pct": (
            sum(contributions) / len(contributions) if contributions else 0.0
        ),
    }


class ParkingSleeveShadowTask(Task):
    """Compute the parking-sleeve sweep; log it (shadow) or emit it (live).

    Class name kept from #157 for wiring/audit continuity. In the default
    ``sleeve.mode="shadow"`` this places NOTHING (runtime-asserted). In
    ``sleeve.mode="live"`` it emits SGOV-floor order intents only — the SPY
    arm stays dark pending its RS-1 §4 pre-registered gate. Either way the
    task is fail-isolated: an internal error is swallowed + counted, never
    breaking the main pipeline (order/exit appends happen atomically at the
    end of the live path, so a mid-computation failure emits nothing).
    """

    name = "ParkingSleeveShadowTask"

    def run(self, ctx: InferenceContext) -> bool | None:
        sleeve_cfg = parking_sleeve_config(ctx)
        if not sleeve_cfg.get("enabled", False):
            return None  # fully inert — no reads, no writes, no counters
        try:
            self._run(ctx, sleeve_cfg)
        except Exception:  # noqa: BLE001 — the sleeve must never break the pipeline
            log.exception("ParkingSleeveShadowTask failed (fail-isolated)")
            ctx.counters["parking_sleeve_error"] = (
                ctx.counters.get("parking_sleeve_error", 0) + 1
            )
        return None

    def _run(self, ctx: InferenceContext, sleeve_cfg: dict) -> None:
        orders_before = len(getattr(ctx, "orders", []) or [])
        exits_before = len(getattr(ctx, "exits", []) or [])

        date_str = getattr(ctx, "today", None)
        date_str = date_str.isoformat() if date_str is not None else None

        mode = str(sleeve_cfg.get("mode", "shadow")).strip().lower()
        if mode not in ("shadow", "live"):
            log.warning(
                "ParkingSleeveShadowTask: unknown sleeve.mode=%r — treating as shadow",
                mode,
            )
            ctx.counters["parking_sleeve_bad_mode"] = (
                ctx.counters.get("parking_sleeve_bad_mode", 0) + 1
            )
            mode = "shadow"

        pv_real = _finite_float(getattr(ctx, "portfolio_value", 0.0), 0.0)
        cash_real = _finite_float(getattr(ctx, "cash", 0.0), 0.0)
        if pv_real <= 0:
            ctx.counters["parking_sleeve_skipped_no_nav"] = (
                ctx.counters.get("parking_sleeve_skipped_no_nav", 0) + 1
            )
            return

        spy_symbol = str(sleeve_cfg.get("spy_symbol", "SPY")).strip().upper() or "SPY"
        sgov_symbol = str(sleeve_cfg.get("sgov_symbol", "SGOV")).strip().upper() or "SGOV"
        prices = getattr(ctx, "prices", None) or {}
        spy_price = _finite_float(prices.get(spy_symbol), 0.0)
        sgov_price = _finite_float(prices.get(sgov_symbol), 0.0)

        path = self._resolve_path(ctx, sleeve_cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with lock_path.open("a+") as lock_fh:
            if fcntl is not None:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                # Idempotency guard, checked INSIDE the lock so a retry or a
                # concurrent run racing on the same host cannot both pass the
                # check before either has appended — see _has_logged_date.
                # Applies to live too: a same-date retry must not re-emit the
                # sweep (the retry replans from CURRENT broker state on the
                # next session; a skipped session is the conservative outcome).
                if _has_logged_date(path, date_str):
                    log.warning(
                        "ParkingSleeveShadowTask: date %s already logged in %s — "
                        "skipping duplicate run (idempotency guard)",
                        date_str, path,
                    )
                    ctx.counters["parking_sleeve_duplicate_date_skipped"] = (
                        ctx.counters.get("parking_sleeve_duplicate_date_skipped", 0) + 1
                    )
                    return
                if mode == "live":
                    self._compute_and_emit_live(
                        ctx, sleeve_cfg, path=path, date_str=date_str,
                        pv_real=pv_real, cash_real=cash_real,
                        spy_symbol=spy_symbol, sgov_symbol=sgov_symbol,
                        sgov_price=sgov_price,
                    )
                else:
                    self._compute_and_log(
                        ctx, sleeve_cfg, path=path, date_str=date_str, mode=mode,
                        pv_real=pv_real, cash_real=cash_real,
                        spy_symbol=spy_symbol, sgov_symbol=sgov_symbol,
                        spy_price=spy_price, sgov_price=sgov_price,
                        orders_before=orders_before, exits_before=exits_before,
                    )
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _compute_and_log(
        self, ctx: InferenceContext, sleeve_cfg: dict, *, path: Path,
        date_str: str | None, mode: str, pv_real: float, cash_real: float,
        spy_symbol: str, sgov_symbol: str, spy_price: float, sgov_price: float,
        orders_before: int, exits_before: int,
    ) -> None:
        """Compute the sweep plan and append it to the shadow log.

        Must only be called while ``path``'s ``.lock`` sibling is held
        exclusively (see ``_run``) — this is the idempotency/concurrency
        critical section: read-last-state, compute, and append are not
        individually safe against a concurrent second caller.
        """
        state = load_last_shadow_state(path)
        if spy_price <= 0 and state["spy_qty"] > 0:
            # Mark a held shadow SPY leg at its last known price rather than
            # zero when today's feed lacks SPY (fail-safe, flagged below).
            spy_price = state["last_spy_price"]

        pending = _pending_buy_invest(ctx)
        regime = getattr(ctx, "regime", None)
        cfg = getattr(ctx, "config", None) or {}
        regime_params = cfg.get("regime_params", {}) if isinstance(cfg, dict) else {}
        regime_p = regime_params.get(regime, {}) if isinstance(regime_params, dict) else {}
        # RAW reserve — deliberately NOT confidence-scaled (module docstring).
        regime_reserve_pct = _finite_float(
            regime_p.get("cash_reserve_pct", 0.0) if isinstance(regime_p, dict) else 0.0,
            0.0,
        )

        sleeve_value_pre = (
            state["spy_qty"] * spy_price if spy_price > 0 else 0.0
        ) + state["sgov_value"]
        shadow_cash = cash_real - state["net_invested"]
        shadow_pv = pv_real + (sleeve_value_pre - state["net_invested"])
        positions_value = max(pv_real - cash_real, 0.0)

        plan = compute_parking_sleeve_plan(
            pv=shadow_pv,
            cash=shadow_cash,
            positions_value=positions_value,
            spy_qty=state["spy_qty"],
            spy_price=spy_price if spy_price > 0 else None,
            sgov_value=state["sgov_value"],
            sgov_price=sgov_price if sgov_price > 0 else None,
            pending_buy_notional=pending,
            regime_cash_reserve_pct=regime_reserve_pct,
            reserve_pv_pct=_finite_float(sleeve_cfg.get("reserve_pv_pct"), 0.05),
            beta_max=_finite_float(sleeve_cfg.get("beta_max"), 0.6),
            beta_pos=_finite_float(sleeve_cfg.get("beta_pos"), 1.0),
            min_trade_notional=_finite_float(sleeve_cfg.get("min_trade_notional"), 50.0),
            spy_symbol=spy_symbol,
            sgov_symbol=sgov_symbol,
        )

        # Roll the shadow book forward through the intended actions.
        new_state = dict(state)
        for action in plan["actions"]:
            notional = _finite_float(action.get("notional"), 0.0)
            sign = 1.0 if action["action"] == "BUY" else -1.0
            new_state["net_invested"] += sign * notional
            if action["symbol"] == spy_symbol:
                qty = _finite_float(action.get("qty"), 0.0)
                new_state["spy_qty"] = max(new_state["spy_qty"] + sign * qty, 0.0)
            else:
                new_state["sgov_value"] = max(new_state["sgov_value"] + sign * notional, 0.0)
        if spy_price > 0:
            new_state["last_spy_price"] = spy_price

        # ── RS-1 §4/§5 monitoring metrics (emission only — never traded on) ──
        sleeve_value_post = (
            new_state["spy_qty"] * spy_price if spy_price > 0 else 0.0
        ) + new_state["sgov_value"]
        contribution_abs = sleeve_value_post - new_state["net_invested"]
        contribution_pct = contribution_abs / shadow_pv if shadow_pv > 0 else 0.0
        hwm = _finite_float(getattr(ctx, "hwm", 0.0), 0.0)
        drawdown_pct = max(0.0, 1.0 - pv_real / hwm) if hwm > 0 else 0.0
        dd_budget_pct = _finite_float(sleeve_cfg.get("dd_budget_pct"), 0.15)
        dd_consumption = drawdown_pct / dd_budget_pct if dd_budget_pct > 0 else 0.0
        new_state["max_dd_budget_consumption_pct"] = max(
            new_state["max_dd_budget_consumption_pct"], dd_consumption,
        )

        book_state = {
            "mode": mode,
            "live_orders_placed": False,
            "pv": round(pv_real, 2),
            "shadow_pv": round(shadow_pv, 2),
            "cash": round(cash_real, 2),
            "shadow_cash": round(shadow_cash, 2),
            "positions_value": round(positions_value, 2),
            "pending_buy_notional": round(pending, 2),
            "regime": regime,
            "regime_cash_reserve_pct": regime_reserve_pct,
            "reserve_operational": round(_finite_float(plan.get("reserve_operational"), 0.0), 2),
            "reserve_regime": round(_finite_float(plan.get("reserve_regime"), 0.0), 2),
            "deployable": round(_finite_float(plan.get("deployable"), 0.0), 2),
            "funding_shortfall": round(_finite_float(plan.get("funding_shortfall"), 0.0), 2),
            "w_pos": round(_finite_float(plan.get("w_pos"), 0.0), 6),
            "w_sleeve": round(_finite_float(plan.get("w_sleeve"), 0.0), 6),
            "sleeve_spy_frac": round(_finite_float(plan.get("sleeve_spy_frac"), 0.0), 6),
            "target_spy_value": round(_finite_float(plan.get("target_spy_value"), 0.0), 2),
            "target_sgov_value": round(_finite_float(plan.get("target_sgov_value"), 0.0), 2),
            "spy_price": spy_price if spy_price > 0 else None,
            "sgov_price": sgov_price if sgov_price > 0 else None,
            "shadow_spy_qty": new_state["spy_qty"],
            "shadow_sgov_value": round(new_state["sgov_value"], 2),
            "net_invested": round(new_state["net_invested"], 2),
            "sleeve_value": round(sleeve_value_post, 2),
            "sleeve_contribution_abs": round(contribution_abs, 2),
            "sleeve_contribution_pct": round(contribution_pct, 6),
            "drawdown_pct": round(drawdown_pct, 6),
            "dd_budget_pct": dd_budget_pct,
            "dd_budget_consumption_pct": round(dd_consumption, 6),
            "max_dd_budget_consumption_pct": round(
                new_state["max_dd_budget_consumption_pct"], 6,
            ),
            "sgov_valuation_mode": SGOV_VALUATION_MODE,
            "blocked": list(plan.get("blocked") or []),
        }

        records: list[dict[str, Any]] = []
        for action in plan["actions"]:
            records.append({
                "record_type": "action",
                "date": date_str,
                "action": action["action"],
                "symbol": action["symbol"],
                "qty": action["qty"],
                "notional": action["notional"],
                "reason": action["reason"],
                "book_state": book_state,
            })
        records.append({
            "record_type": "summary",
            "date": date_str,
            "action": "hold" if not plan["actions"] else "summary",
            "symbol": None,
            "qty": None,
            "notional": 0.0,
            "reason": str(plan.get("reason")),
            "book_state": book_state,
            "shadow_state": {k: round(float(v), 6) for k, v in new_state.items()},
        })

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in records:
                fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")

        ctx._parking_sleeve_last = records[-1]  # noqa: SLF001
        ctx._parking_sleeve_log_path = str(path)  # noqa: SLF001
        ctx.counters["parking_sleeve_shadow_rows"] = (
            ctx.counters.get("parking_sleeve_shadow_rows", 0) + len(records)
        )
        ctx.counters["parking_sleeve_intended_actions"] = (
            ctx.counters.get("parking_sleeve_intended_actions", 0) + len(plan["actions"])
        )
        log.info(
            "ParkingSleeveShadowTask: %d intended action(s) reason=%s "
            "deployable=$%.0f spy_frac=%.3f (SHADOW — nothing placed) → %s",
            len(plan["actions"]), plan.get("reason"),
            _finite_float(plan.get("deployable"), 0.0),
            _finite_float(plan.get("sleeve_spy_frac"), 0.0), path,
        )

        # Shadow invariant: this task must never emit real orders or exits.
        assert len(getattr(ctx, "orders", []) or []) == orders_before, \
            "parking sleeve shadow mutated ctx.orders"
        assert len(getattr(ctx, "exits", []) or []) == exits_before, \
            "parking sleeve shadow mutated ctx.exits"

    # ── live (SGOV floor) path ────────────────────────────────────────────

    def _compute_and_emit_live(
        self, ctx: InferenceContext, sleeve_cfg: dict, *, path: Path,
        date_str: str | None, pv_real: float, cash_real: float,
        spy_symbol: str, sgov_symbol: str, sgov_price: float,
    ) -> None:
        """Plan against REAL broker state and emit SGOV-floor order intents.

        Must only be called while ``path``'s ``.lock`` sibling is held (see
        ``_run``). The SPY arm is structurally dark here: the planner runs
        with ``sgov_only=True`` and ``spy_qty=0``, and a defensive guard
        drops any non-SGOV action — live SPY exposure requires the RS-1 §4
        pre-registered gate and is a separate change by design.

        Intents are built first and appended to ``ctx.exits``/``ctx.orders``
        in one block at the end, so a failure mid-computation emits nothing.
        """
        cfg = getattr(ctx, "config", None) or {}
        pending = _pending_buy_invest(ctx)
        regime = getattr(ctx, "regime", None)
        regime_params = cfg.get("regime_params", {}) if isinstance(cfg, dict) else {}
        regime_p = regime_params.get(regime, {}) if isinstance(regime_params, dict) else {}
        # RAW reserve — deliberately NOT confidence-scaled (module docstring).
        rr = min(max(_finite_float(
            regime_p.get("cash_reserve_pct", 0.0) if isinstance(regime_p, dict) else 0.0,
            0.0,
        ), 0.0), 1.0)
        reserve_pv_pct = min(max(
            _finite_float(sleeve_cfg.get("reserve_pv_pct"), 0.05), 0.0), 1.0)
        max_sleeve_pct = _finite_float(
            sleeve_cfg.get("max_sleeve_pct"), DEFAULT_MAX_SLEEVE_PCT_LIVE)

        # REAL broker SGOV book — the cumulative cross-session truth.
        holdings = getattr(ctx, "holdings", None) or {}
        hs = holdings.get(sgov_symbol)
        sgov_shares = max(
            _finite_float(getattr(hs, "shares", 0.0), 0.0) if hs is not None else 0.0,
            0.0,
        )
        entry_price = (
            _finite_float(getattr(hs, "entry_price", 0.0), 0.0) if hs is not None else 0.0
        )
        basis_before = entry_price * sgov_shares if entry_price > 0 else (
            sgov_shares * sgov_price if sgov_price > 0 else 0.0
        )

        blocked: list[str] = []
        live_exits: list[tuple[str, Any]] = []
        live_orders: list[dict[str, Any]] = []

        # Another emitter already touched the sleeve symbol this bar —
        # never stack a second intent on it (mirrors BenchmarkSleeveTask).
        if sgov_symbol in _existing_buy_tickers(ctx) or (
            sgov_symbol in _existing_exit_tickers(ctx)
        ):
            blocked.append("live_symbol_already_touched")
            ctx.counters["parking_sleeve_live_symbol_already_touched"] = (
                ctx.counters.get("parking_sleeve_live_symbol_already_touched", 0) + 1
            )
            log.warning(
                "ParkingSleeveShadowTask[live]: %s already has an order/exit "
                "this bar — sleeve stands down", sgov_symbol,
            )
            self._write_live_records(
                ctx, sleeve_cfg, path=path, date_str=date_str, plan=None,
                pv_real=pv_real, cash_real=cash_real, pending=pending, rr=rr,
                regime=regime, sgov_symbol=sgov_symbol, sgov_price=sgov_price,
                sgov_shares=sgov_shares, basis_before=basis_before,
                live_exits=[], live_orders=[], blocked=blocked,
                reason="live_symbol_already_touched",
            )
            return

        reserve_operational = reserve_pv_pct * pv_real + pending
        reserve_regime = rr * pv_real
        funding_shortfall = max(0.0, reserve_operational + reserve_regime - cash_real)

        if sgov_price <= 0:
            # FAIL-CLOSED: the umbrella daily price fetch does not cover SGOV
            # yet (verified 2026-07-10: no SGOV bars in either OHLCV store).
            # No buy is ever emitted without a positive price. If cash is
            # actually needed (reserve/pending shortfall, or a BEAR full
            # sweep-off) the FULL position is liquidated — a full exit needs
            # no price, and freeing cash outranks keeping the sleeve parked.
            blocked.append("sgov_price_missing_live_fail_closed")
            ctx.counters["parking_sleeve_live_missing_sgov_price"] = (
                ctx.counters.get("parking_sleeve_live_missing_sgov_price", 0) + 1
            )
            log.error(
                "ParkingSleeveShadowTask[live]: no price for %s — FAIL-CLOSED "
                "(no sleeve buys; umbrella daily price fetch must add %s bars "
                "before live parking can deploy). shortfall=$%.2f held=%s",
                sgov_symbol, sgov_symbol, funding_shortfall, sgov_shares,
            )
            fail_closed_plan = {
                "reserve_operational": reserve_operational,
                "reserve_regime": reserve_regime,
                "funding_shortfall": funding_shortfall,
                "deployable": 0.0,
                "max_sleeve_pct": min(max(max_sleeve_pct, 0.0), 1.0),
                "max_sleeve_value_cap": min(max(max_sleeve_pct, 0.0), 1.0) * pv_real,
            }
            reason = "sgov_price_missing_fail_closed"
            if sgov_shares >= 1 and (funding_shortfall > 0.0 or rr >= 1.0):
                from renquant_pipeline.kernel.exits import ExitSignal  # noqa: PLC0415
                sig = ExitSignal(
                    should_exit=True,
                    reason=(
                        "parking sleeve fail-closed liquidation: "
                        f"{sgov_symbol} price missing while "
                        f"${funding_shortfall:.2f} cash shortfall / regime "
                        f"reserve {rr:.2f} demands cash — full exit"
                    ),
                    exit_type="parking_sleeve_sweep",
                    quantity=None,  # FULL exit — needs no price
                )
                self._stamp_exit_source(sig)
                live_exits.append((sgov_symbol, sig))
                reason = "sgov_price_missing_fail_closed_full_exit"
            for item in live_exits:
                ctx.exits.append(item)
            ctx.counters["parking_sleeve_live_exits"] = (
                ctx.counters.get("parking_sleeve_live_exits", 0) + len(live_exits)
            )
            self._write_live_records(
                ctx, sleeve_cfg, path=path, date_str=date_str,
                plan=fail_closed_plan,
                pv_real=pv_real, cash_real=cash_real, pending=pending, rr=rr,
                regime=regime, sgov_symbol=sgov_symbol, sgov_price=0.0,
                sgov_shares=sgov_shares, basis_before=basis_before,
                live_exits=live_exits, live_orders=[], blocked=blocked,
                reason=reason,
            )
            return

        sgov_value = sgov_shares * sgov_price
        # The sleeve is NOT an alpha position — exclude it from w_pos so the
        # β budget sees only real single-name exposure (RS-1 §3 narrow
        # exclusion; SGOV itself is the near-zero-beta leg).
        positions_value = max(pv_real - cash_real - sgov_value, 0.0)

        plan = compute_parking_sleeve_plan(
            pv=pv_real,
            cash=cash_real,
            positions_value=positions_value,
            spy_qty=0.0,                       # SPY arm is DARK in live mode
            spy_price=None,
            sgov_value=sgov_value,
            sgov_price=sgov_price,
            pending_buy_notional=pending,
            regime_cash_reserve_pct=rr,
            reserve_pv_pct=reserve_pv_pct,
            beta_max=_finite_float(sleeve_cfg.get("beta_max"), 0.6),
            beta_pos=_finite_float(sleeve_cfg.get("beta_pos"), 1.0),
            min_trade_notional=_finite_float(sleeve_cfg.get("min_trade_notional"), 50.0),
            max_sleeve_pct=max_sleeve_pct,
            sgov_only=True,
            spy_symbol=spy_symbol,
            sgov_symbol=sgov_symbol,
        )
        blocked.extend(plan.get("blocked") or [])

        buy_gated = (
            bool(getattr(ctx, "buy_blocked", False))
            or bool(getattr(ctx, "skip_buys", False))
            or bool(getattr(ctx, "bear_only", False))
        )
        wash_days = 30
        if isinstance(cfg, dict):
            wash_days = int(_finite_float(cfg.get("wash_sale_days"), 30.0))

        for action in plan["actions"]:
            symbol = str(action.get("symbol") or "")
            if symbol != sgov_symbol:
                # Structural SPY-dark guard — must be unreachable with
                # sgov_only=True + spy_qty=0; never emit and say so loudly.
                blocked.append(f"live_non_sgov_action_dropped:{symbol}")
                ctx.counters["parking_sleeve_live_non_sgov_dropped"] = (
                    ctx.counters.get("parking_sleeve_live_non_sgov_dropped", 0) + 1
                )
                log.error(
                    "ParkingSleeveShadowTask[live]: dropped non-SGOV action %r "
                    "(SPY arm is not authorized for live exposure)", action,
                )
                continue
            if action["action"] == "SELL":
                sig = self._build_live_sell(
                    ctx, action, sgov_symbol=sgov_symbol,
                    sgov_price=sgov_price, sgov_shares=sgov_shares,
                )
                if sig is not None:
                    live_exits.append((sgov_symbol, sig))
                continue
            # BUY — gates first (sells above are never gated).
            if buy_gated:
                blocked.append("live_buy_gates_blocked")
                ctx.counters["parking_sleeve_live_buy_gated"] = (
                    ctx.counters.get("parking_sleeve_live_buy_gated", 0) + 1
                )
                continue
            from renquant_pipeline.kernel.selection import (  # noqa: PLC0415
                is_wash_sale_blocked_with_cost,
            )
            ws_blocked, ws_reason, _ = is_wash_sale_blocked_with_cost(
                sgov_symbol, ctx.today,
                getattr(ctx, "last_sell_dates", None) or {},
                getattr(ctx, "last_sell_pls", None) or {},
                wash_days,
            )
            if ws_blocked:
                # A recent SGOV LOSS sale would wash under §1091 — same
                # engine, same verdict as any single-name buy. Gain sales
                # and stale sales pass through the engine unblocked.
                blocked.append("sgov_wash_sale_blocked")
                ctx.counters["parking_sleeve_live_wash_sale_blocked"] = (
                    ctx.counters.get("parking_sleeve_live_wash_sale_blocked", 0) + 1
                )
                log.warning(
                    "ParkingSleeveShadowTask[live]: SGOV buy wash-sale "
                    "blocked — %s", ws_reason,
                )
                continue
            order = self._build_live_buy(
                ctx, action, plan=plan, sgov_symbol=sgov_symbol,
                sgov_price=sgov_price, sgov_value=sgov_value,
                pv_real=pv_real, cash_real=cash_real,
            )
            if order is not None:
                live_orders.append(order)

        # Atomic append — nothing above mutated ctx.
        for item in live_exits:
            ctx.exits.append(item)
        for order in live_orders:
            ctx.orders.append(order)
        ctx.counters["parking_sleeve_live_exits"] = (
            ctx.counters.get("parking_sleeve_live_exits", 0) + len(live_exits)
        )
        ctx.counters["parking_sleeve_live_orders"] = (
            ctx.counters.get("parking_sleeve_live_orders", 0) + len(live_orders)
        )
        log.info(
            "ParkingSleeveShadowTask[live]: emitted %d sell(s) + %d buy(s) "
            "reason=%s deployable=$%.0f shortfall=$%.0f (SGOV floor, SPY dark)",
            len(live_exits), len(live_orders), plan.get("reason"),
            _finite_float(plan.get("deployable"), 0.0),
            _finite_float(plan.get("funding_shortfall"), 0.0),
        )
        self._write_live_records(
            ctx, sleeve_cfg, path=path, date_str=date_str, plan=plan,
            pv_real=pv_real, cash_real=cash_real, pending=pending, rr=rr,
            regime=regime, sgov_symbol=sgov_symbol, sgov_price=sgov_price,
            sgov_shares=sgov_shares, basis_before=basis_before,
            live_exits=live_exits, live_orders=live_orders, blocked=blocked,
            reason=str(plan.get("reason")),
        )

    @staticmethod
    def _stamp_exit_source(sig: Any) -> None:
        sig.source_job = "ParkingSleeveJob"
        sig.source_task = "ParkingSleeveShadowTask"
        sig.order_source = "ParkingSleeveJob.ParkingSleeveShadowTask"
        sig.source = sig.order_source

    def _build_live_sell(
        self, ctx: InferenceContext, action: dict, *, sgov_symbol: str,
        sgov_price: float, sgov_shares: float,
    ) -> Any | None:
        """Translate a planned SGOV SELL into an ExitSignal.

        Shares are sized with the sell-proceeds cost multiplier so the cash
        actually freed (net of spread/fees) still covers the planned
        notional — the free-before-need invariant survives friction.
        """
        if sgov_shares < 1:
            return None
        notional = _finite_float(action.get("notional"), 0.0)
        if notional <= 0:
            return None
        unit_proceeds = sgov_price * _sell_proceeds_multiplier(
            getattr(ctx, "config", None) or {})
        if unit_proceeds <= 0:
            qty = int(sgov_shares)
        else:
            qty = min(int(math.ceil(notional / unit_proceeds)), int(sgov_shares))
        if qty < 1:
            return None
        from renquant_pipeline.kernel.exits import ExitSignal  # noqa: PLC0415
        full = qty >= int(sgov_shares)
        sig = ExitSignal(
            should_exit=True,
            reason=f"parking sleeve: {action.get('reason')}",
            exit_type="parking_sleeve_sweep",
            quantity=None if full else float(qty),
        )
        self._stamp_exit_source(sig)
        return sig

    def _build_live_buy(
        self, ctx: InferenceContext, action: dict, *, plan: dict,
        sgov_symbol: str, sgov_price: float, sgov_value: float,
        pv_real: float, cash_real: float,
    ) -> dict | None:
        """Translate a planned SGOV BUY into an attributed order dict.

        Whole shares, priced with the buy-cost multiplier, and re-clamped so
        the invest can never dig into the operational/regime reserves or the
        pending main-strategy buys (belt-and-suspenders on top of the
        planner's ``deployable`` arithmetic — the liquidity invariant is
        enforced twice).
        """
        notional = _finite_float(action.get("notional"), 0.0)
        invest_cap = max(
            0.0,
            cash_real
            - _finite_float(plan.get("reserve_operational"), 0.0)
            - _finite_float(plan.get("reserve_regime"), 0.0),
        )
        buy_value = min(notional, invest_cap)
        unit_cost = sgov_price * _buy_cost_multiplier(getattr(ctx, "config", None) or {})
        qty = int(buy_value // unit_cost) if unit_cost > 0 else 0
        if qty < 1:
            ctx.counters["parking_sleeve_live_buy_dust_skipped"] = (
                ctx.counters.get("parking_sleeve_live_buy_dust_skipped", 0) + 1
            )
            return None
        invest = qty * sgov_price
        target_pct = (sgov_value + invest) / pv_real if pv_real > 0 else 0.0
        return stamp_order_attribution({
            "ticker": sgov_symbol,
            "shares": float(qty),
            "price": sgov_price,
            "invest": invest,
            "target_pct": target_pct,
            "regime": getattr(ctx, "regime", None),
            "confidence": getattr(ctx, "confidence", None),
            "conviction": 1.0,
            "sigma_mult": 1.0,
            "rank_score": None,
            "rs_score": 0.0,
            "panel_score": None,
            "sigma": None,
            "mu": None,
            "kelly_target_pct": None,
            "detail": "parking_sleeve_sgov_floor",
            "order_type": "PARKING_SLEEVE_BUY",
        }, ctx=ctx, source_job="ParkingSleeveJob",
            source_task="ParkingSleeveShadowTask",
            acceptance_reason="idle_cash_to_sgov_parking_floor",
            decision_inputs={
                "reason": plan.get("reason"),
                "deployable": _finite_float(plan.get("deployable"), 0.0),
                "reserve_operational": _finite_float(plan.get("reserve_operational"), 0.0),
                "reserve_regime": _finite_float(plan.get("reserve_regime"), 0.0),
                "max_sleeve_pct": _finite_float(plan.get("max_sleeve_pct"), 0.0),
                "max_sleeve_value_cap": _finite_float(plan.get("max_sleeve_value_cap"), 0.0),
                "invest_cap": invest_cap,
                "sgov_value_before": sgov_value,
            })

    def _write_live_records(
        self, ctx: InferenceContext, sleeve_cfg: dict, *, path: Path,
        date_str: str | None, plan: dict | None, pv_real: float,
        cash_real: float, pending: float, rr: float, regime: Any,
        sgov_symbol: str, sgov_price: float, sgov_shares: float,
        basis_before: float, live_exits: list, live_orders: list,
        blocked: list[str], reason: str,
    ) -> None:
        """Append the live session's rows to the JSONL (same schema as shadow).

        ``shadow_state`` mirrors the REAL post-trade book (SGOV at cost
        basis, SPY always 0) so a later flip back to shadow never inherits a
        stale shadow book. The running ``max_dd_budget_consumption_pct`` is
        carried across sessions through the same field as shadow mode.
        """
        prior = load_last_shadow_state(path)

        sold_qty = 0.0
        for _ticker, sig in live_exits:
            q = getattr(sig, "quantity", None)
            sold_qty += sgov_shares if q is None else min(float(q), sgov_shares)
        bought_qty = sum(_finite_float(o.get("shares"), 0.0) for o in live_orders)
        buy_invest = sum(_finite_float(o.get("invest"), 0.0) for o in live_orders)

        shares_after = max(sgov_shares - sold_qty, 0.0) + bought_qty
        basis_per_share = basis_before / sgov_shares if sgov_shares > 0 else 0.0
        basis_after = max(basis_before - sold_qty * basis_per_share, 0.0) + buy_invest
        value_after = shares_after * sgov_price if sgov_price > 0 else basis_after

        # Mark-to-market contribution of the REAL pre-trade holding
        # (trading at the current price does not change it).
        contribution_abs = (
            sgov_shares * sgov_price - basis_before if sgov_price > 0 else 0.0
        )
        contribution_pct = contribution_abs / pv_real if pv_real > 0 else 0.0
        hwm = _finite_float(getattr(ctx, "hwm", 0.0), 0.0)
        drawdown_pct = max(0.0, 1.0 - pv_real / hwm) if hwm > 0 else 0.0
        dd_budget_pct = _finite_float(sleeve_cfg.get("dd_budget_pct"), 0.15)
        dd_consumption = drawdown_pct / dd_budget_pct if dd_budget_pct > 0 else 0.0
        max_dd = max(
            _finite_float(prior.get("max_dd_budget_consumption_pct"), 0.0),
            dd_consumption,
        )

        plan = plan or {}
        book_state = {
            "mode": "live",
            "live_orders_placed": bool(live_exits or live_orders),
            "pv": round(pv_real, 2),
            "shadow_pv": round(pv_real, 2),      # no shadow book in live mode
            "cash": round(cash_real, 2),
            "shadow_cash": round(cash_real, 2),  # no shadow book in live mode
            "positions_value": round(max(pv_real - cash_real, 0.0), 2),
            "pending_buy_notional": round(pending, 2),
            "regime": regime,
            "regime_cash_reserve_pct": rr,
            "reserve_operational": round(_finite_float(plan.get("reserve_operational"), 0.0), 2),
            "reserve_regime": round(_finite_float(plan.get("reserve_regime"), 0.0), 2),
            "deployable": round(_finite_float(plan.get("deployable"), 0.0), 2),
            "funding_shortfall": round(_finite_float(plan.get("funding_shortfall"), 0.0), 2),
            "w_pos": round(_finite_float(plan.get("w_pos"), 0.0), 6),
            "w_sleeve": round(_finite_float(plan.get("w_sleeve"), 0.0), 6),
            "sleeve_spy_frac": 0.0,               # SPY arm dark in live mode
            "target_spy_value": 0.0,
            "target_sgov_value": round(_finite_float(plan.get("target_sgov_value"), 0.0), 2),
            "spy_price": None,
            "sgov_price": sgov_price if sgov_price > 0 else None,
            "shadow_spy_qty": 0.0,
            "shadow_sgov_value": round(basis_after, 2),
            "net_invested": round(basis_after, 2),
            "sleeve_value": round(value_after, 2),
            "sleeve_contribution_abs": round(contribution_abs, 2),
            "sleeve_contribution_pct": round(contribution_pct, 6),
            "drawdown_pct": round(drawdown_pct, 6),
            "dd_budget_pct": dd_budget_pct,
            "dd_budget_consumption_pct": round(dd_consumption, 6),
            "max_dd_budget_consumption_pct": round(max_dd, 6),
            "sgov_valuation_mode": SGOV_VALUATION_MODE_LIVE,
            "blocked": list(blocked),
            # live-only telemetry
            "live_sgov_shares_before": sgov_shares,
            "live_sgov_shares_after": shares_after,
            "max_sleeve_pct": _finite_float(plan.get("max_sleeve_pct"), None),
            "max_sleeve_value_cap": _finite_float(plan.get("max_sleeve_value_cap"), 0.0),
            "sleeve_cap_bound": bool(plan.get("sleeve_cap_bound", False)),
        }

        records: list[dict[str, Any]] = []
        for _ticker, sig in live_exits:
            q = getattr(sig, "quantity", None)
            qty = int(sgov_shares) if q is None else int(q)
            records.append({
                "record_type": "action",
                "date": date_str,
                "action": "SELL",
                "symbol": sgov_symbol,
                "qty": qty,
                "notional": round(qty * sgov_price, 2) if sgov_price > 0 else None,
                "reason": getattr(sig, "reason", reason),
                "book_state": book_state,
            })
        for order in live_orders:
            records.append({
                "record_type": "action",
                "date": date_str,
                "action": "BUY",
                "symbol": order.get("ticker"),
                "qty": int(_finite_float(order.get("shares"), 0.0)),
                "notional": round(_finite_float(order.get("invest"), 0.0), 2),
                "reason": reason,
                "book_state": book_state,
            })
        new_state = {
            "spy_qty": 0.0,
            "sgov_value": basis_after,
            "net_invested": basis_after,
            "last_spy_price": 0.0,
            "max_dd_budget_consumption_pct": max_dd,
        }
        records.append({
            "record_type": "summary",
            "date": date_str,
            "action": "hold" if not (live_exits or live_orders) else "summary",
            "symbol": None,
            "qty": None,
            "notional": 0.0,
            "reason": reason,
            "book_state": book_state,
            "shadow_state": {k: round(float(v), 6) for k, v in new_state.items()},
        })

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for row in records:
                fh.write(json.dumps(row, default=str, sort_keys=True) + "\n")

        ctx._parking_sleeve_last = records[-1]  # noqa: SLF001
        ctx._parking_sleeve_log_path = str(path)  # noqa: SLF001
        ctx.counters["parking_sleeve_live_rows"] = (
            ctx.counters.get("parking_sleeve_live_rows", 0) + len(records)
        )
        ctx.counters["parking_sleeve_intended_actions"] = (
            ctx.counters.get("parking_sleeve_intended_actions", 0)
            + len(live_exits) + len(live_orders)
        )

    @staticmethod
    def _resolve_path(ctx: InferenceContext, sleeve_cfg: dict) -> Path:
        raw = sleeve_cfg.get("log_path") or DEFAULT_LOG_PATH
        path = Path(str(raw))
        if path.is_absolute():
            return path
        root = (
            getattr(ctx, "strategy_dir", None)
            or (getattr(ctx, "config", None) or {}).get("_strategy_dir")
            or "."
        )
        return Path(root) / path
