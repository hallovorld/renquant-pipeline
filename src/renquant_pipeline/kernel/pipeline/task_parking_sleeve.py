"""ParkingSleeveShadowTask — S7 lane-B parking sleeve (β-budgeted SPY/SGOV), shadow mode.

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
* **Shadow only.** In ``sleeve.mode = "shadow"`` the task computes the
  intended sweep/fund orders and appends them to a dedicated JSONL under
  ``logs/`` — schema ``{date, action, symbol, qty, notional, reason,
  book_state}`` — placing NOTHING (never touches ``ctx.orders`` /
  ``ctx.exits``; runtime-asserted). ``sleeve.mode = "live"`` is deliberately
  NOT implemented in this change: RS-1 §4 requires a pre-registered
  cash-vs-SGOV-vs-SPY comparison plus an explicit capital-authorization
  decision before real exposure; until then live mode falls back to shadow
  logging with a warning + counter.
* The shadow sleeve book persists across sessions inside the JSONL itself
  (the last ``record_type == "summary"`` row carries ``shadow_state``), so
  the 10-session shadow AC exercises the incremental sweep / sell-to-fund
  round-trip, not a fresh full sweep every day.

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

from .context import InferenceContext
from .pipeline import Task
from .task_benchmark_sleeve import _finite_float, _pending_buy_invest

log = logging.getLogger("kernel.pipeline.parking_sleeve")

DEFAULT_LOG_PATH = "logs/parking_sleeve_shadow.jsonl"

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
    spy_symbol: str = "SPY",
    sgov_symbol: str = "SGOV",
) -> dict[str, Any]:
    """Pure β-budgeted sweep/fund planner (no ctx, no I/O — unit-testable).

    Returns a dict with the intended ``actions`` (sells FIRST, then buys)
    and every intermediate quantity needed for the shadow log's
    ``book_state``. Monetary inputs are dollars; ``sgov_value`` is the
    current SGOV leg at cost.
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

    spy_value = spy_qty * spy_price if spy_price > 0 else 0.0
    sleeve_value = spy_value + sgov_value

    reserve_operational = reserve_pv_pct * pv + pending_buy_notional
    reserve_regime = rr * pv
    deployable = max(0.0, cash + sleeve_value - reserve_operational - reserve_regime)
    funding_shortfall = max(0.0, reserve_operational + reserve_regime - cash)

    w_pos = positions_value / pv
    w_sleeve = deployable / pv
    if w_sleeve <= 0.0:
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


class ParkingSleeveShadowTask(Task):
    """Compute + log the intended parking-sleeve orders. Places NOTHING."""

    name = "ParkingSleeveShadowTask"

    def run(self, ctx: InferenceContext) -> bool | None:
        sleeve_cfg = parking_sleeve_config(ctx)
        if not sleeve_cfg.get("enabled", False):
            return None  # fully inert — no reads, no writes, no counters
        try:
            self._run(ctx, sleeve_cfg)
        except Exception:  # noqa: BLE001 — a shadow logger must never break the pipeline
            log.exception("ParkingSleeveShadowTask failed (shadow only — no orders affected)")
            ctx.counters["parking_sleeve_error"] = (
                ctx.counters.get("parking_sleeve_error", 0) + 1
            )
        return None

    def _run(self, ctx: InferenceContext, sleeve_cfg: dict) -> None:
        orders_before = len(getattr(ctx, "orders", []) or [])
        exits_before = len(getattr(ctx, "exits", []) or [])

        mode = str(sleeve_cfg.get("mode", "shadow")).strip().lower()
        if mode == "live":
            # RS-1 §4: live enablement requires the pre-registered
            # cash/SGOV/SPY comparison + a recorded capital authorization.
            # The order-placing path intentionally does not exist yet.
            log.warning(
                "ParkingSleeveShadowTask: sleeve.mode='live' is NOT implemented "
                "(RS-1 §4 authorization bar) — falling back to shadow logging",
            )
            ctx.counters["parking_sleeve_live_mode_unimplemented"] = (
                ctx.counters.get("parking_sleeve_live_mode_unimplemented", 0) + 1
            )
        elif mode != "shadow":
            log.warning(
                "ParkingSleeveShadowTask: unknown sleeve.mode=%r — treating as shadow",
                mode,
            )
            ctx.counters["parking_sleeve_bad_mode"] = (
                ctx.counters.get("parking_sleeve_bad_mode", 0) + 1
            )

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
            "blocked": list(plan.get("blocked") or []),
        }

        date_str = getattr(ctx, "today", None)
        date_str = date_str.isoformat() if date_str is not None else None
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
