"""Deployment Governor pipeline integration (L1+L2+L3, flag OFF by default).

Implements D2–D4 of the Deployment Governor RFC (orchestrator
``doc/design/2026-07-09-deployment-governor-rfc.md``) behind the top-level
``deployment_governor`` config block:

    {"enabled": false,
     "e_ceil_by_regime": {"BULL_CALM": 0.95, "BULL_VOLATILE": 0.7,
                          "CHOPPY": 0.6, "BEAR": 0.35},
     "hysteresis_band": 0.05,
     "kelly_fraction": 0.3,
     "mu_shrinkage": 0.0,
     "top_k": 8,
     "max_step_per_session": 0.15}

``enabled`` absent/false ⇒ BYTE-IDENTICAL pipeline behaviour (pinned by
``tests/test_governor_sizing_integration.py``). When enabled,
``SizeAndEmitTask`` calls :func:`run_governor_sizing` INSTEAD of the legacy
multiplicative sizing stack:

* L1 (:mod:`renquant_pipeline.kernel.deployment_governor`) computes the
  session target gross exposure E* over the ALREADY-ADMITTED slate
  (``ctx._selected``, post greedy-loop + signal-direction gate) plus the
  held book. NO admission gate or exit logic is touched.
* L2 (:mod:`renquant_pipeline.kernel.deployment_allocator`) computes
  down-only target weights under per-name / sector / correlation caps and
  the no-buy (wash-sale) + no-sell (min-hold / §1091) masks.
* L3 (this module) executes the weight deltas with whole-share greedy
  rounding in conviction order plus a residual-cash second pass that
  re-offers leftover cash to the next affordable name (generalizing the
  one-share deferred-rescue pattern, S6 A-3). Exit legs come from weight
  deltas: each is charged its lot tax drag (existing ``tax_drag()``)
  plus linear transaction cost, and an exit+entry pair is emitted only
  when the post-cost improvement is positive (RFC §1.3).

Fail-closed: a model fault (no usable μ̂/σ̂ moments on a non-empty slate,
missing held-name price, unmapped regime) makes the Governor emit NO
target — :func:`run_governor_sizing` returns ``False`` and the caller
falls back to the legacy sizing path unchanged.
"""
from __future__ import annotations

import datetime
import logging
import math
from typing import Any

from renquant_pipeline.kernel.deployment_allocator import allocate_down_only
from renquant_pipeline.kernel.deployment_governor import (
    compute_session_target_exposure,
    shrunk_kelly_raw,
)

from .order_attribution import stamp_order_attribution
from .signal_direction import long_signal_ok_for_object

log = logging.getLogger("kernel.pipeline.governor_sizing")

# Schema defaults — MUST stay in sync with the strategy-104 D5 config PR
# (the block above is the frozen contract; absent keys mean these values).
GOVERNOR_DEFAULTS: dict[str, Any] = {
    "e_ceil_by_regime": {
        "BULL_CALM": 0.95,
        "BULL_VOLATILE": 0.7,
        "CHOPPY": 0.6,
        "BEAR": 0.35,
    },
    "hysteresis_band": 0.05,
    "kelly_fraction": 0.3,
    "mu_shrinkage": 0.0,
    "top_k": 8,
    "max_step_per_session": 0.15,
}

# Protocol §1.1 frozen convention: 5 bps per side. Used only when the
# strategy config does not already define rotation.transaction_cost_pct
# (single source of truth for the linear cost when it exists).
_DEFAULT_TXN_COST_PER_SIDE = 0.0005

_EPS = 1e-9


def governor_config(config: dict | None) -> dict | None:
    blk = (config or {}).get("deployment_governor")
    return blk if isinstance(blk, dict) else None


def governor_enabled(config: dict | None) -> bool:
    """True iff the top-level ``deployment_governor.enabled`` flag is on."""
    blk = governor_config(config)
    return bool(blk and blk.get("enabled", False))


def governor_owns_sizing(ctx: Any) -> bool:
    """True iff the Governor actually OWNED this session's sizing decision.

    Set by :func:`run_governor_sizing` on every non-fault path (including
    a hysteresis hold — "no reallocation" IS a sizing decision). When the
    Governor owns sizing it owns ALL of it: ``TopUpHeldTask`` /
    ``TrimHeldTask`` must NO-OP (structurally, not by config discipline) —
    a live top-up would double-add to positions the allocator already
    sized and pollute S1 shadow data.

    False when the flag is off (attribute never set — byte-identical
    legacy behaviour) AND on fault-fallback sessions (Governor emitted no
    target → the legacy path ran, so legacy top-up/trim semantics are
    fully preserved).
    """
    return bool(getattr(ctx, "_governor_owns_sizing", False))


def _cfg(gov_cfg: dict, key: str) -> Any:
    value = gov_cfg.get(key)
    return GOVERNOR_DEFAULTS[key] if value is None else value


def _block(ctx: Any, ticker: str, reason: str) -> None:
    """Same block-stamping contract as SizeAndEmitTask's local helper."""
    blocked_map = getattr(ctx, "_blocked_by_ticker", None)
    if blocked_map is None:
        blocked_map = {}
        ctx._blocked_by_ticker = blocked_map  # noqa: SLF001
    blocked_map.setdefault(ticker, reason)
    key = f"selection_{reason.split(':', 1)[0]}"
    ctx.counters[key] = ctx.counters.get(key, 0) + 1


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _coerce_date(value: Any) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except (ValueError, TypeError):
            return None
    return None


def run_governor_sizing(ctx: Any, gov_cfg: dict) -> bool:
    """Size the session to Governor/allocator target weights.

    Returns True when the Governor OWNED the sizing decision this session
    (orders/exits emitted, possibly none on a hysteresis hold or weak
    slate). Returns False on a FAULT — the caller must fall back to the
    legacy sizing path (RFC §2.1 fail-closed semantics).
    """
    config = ctx.config or {}
    pv = _finite(getattr(ctx, "portfolio_value", None))
    if pv is None or pv <= 0:
        return _fault(ctx, "invalid_portfolio_value")

    prices = getattr(ctx, "prices", {}) or {}
    regime = getattr(ctx, "regime", None)
    regime_p = (config.get("regime_params", {}) or {}).get(regime, {}) or {}
    cap_pct = float(regime_p.get("max_position_pct", 0.15))

    # ── Already-admitted candidates (admission chain untouched) ───────
    # The signal-direction gate historically runs INSIDE the sizing task,
    # so it is applied here identically before any name reaches the
    # Governor (same block reasons as the legacy path).
    candidates: dict[str, Any] = {}
    for ticker in list(getattr(ctx, "_selected", []) or []):  # noqa: SLF001
        c = next((c for c in ctx.ranked if c.ticker == ticker), None)
        signal_ok, signal_reason = long_signal_ok_for_object(c, config)
        if not signal_ok:
            log.info(
                "governor_sizing: %s BLOCKED new long — %s "
                "(panel_score=%s expected_return=%s)",
                ticker, signal_reason,
                getattr(c, "panel_score", None) if c is not None else None,
                getattr(c, "expected_return", None) if c is not None else None,
            )
            _block(ctx, ticker, signal_reason)
            continue
        price = _finite(prices.get(ticker))
        if price is None or price <= 0:
            log.warning("governor_sizing: bad price (%s) for %s — skipping",
                        prices.get(ticker), ticker)
            _block(ctx, ticker, "size_bad_price")
            continue
        candidates[ticker] = c

    # ── Held book (exit legs owned by the untouched exit stack are
    #    excluded: names already exiting/rotating this bar) ────────────
    exiting: set[str] = set()
    for e in (getattr(ctx, "exits", []) or []):
        if isinstance(e, tuple) and len(e) == 2:
            exiting.add(e[0])
        else:
            t = getattr(e, "ticker", None)
            if t is not None:
                exiting.add(t)
    for p in (getattr(ctx, "rotations", []) or []):
        exiting.add(p.sell_ticker)
        exiting.add(p.buy_ticker)

    held: dict[str, Any] = {}
    current_w: dict[str, float] = {}
    for ticker, hs in (getattr(ctx, "holdings", {}) or {}).items():
        if ticker in exiting:
            continue
        shares = _finite(getattr(hs, "shares", 0.0)) or 0.0
        if shares <= 0:
            continue
        price = _finite(prices.get(ticker))
        if price is None or price <= 0:
            # A held name without a usable price makes the book's gross
            # exposure unknowable — the Governor must not resize on a
            # broken feed. Fail closed to the legacy path.
            return _fault(ctx, f"held_price_missing:{ticker}")
        held[ticker] = hs
        current_w[ticker] = shares * price / pv
    e_current = float(sum(current_w.values()))

    # ── Moments + shrunk-Kelly raws over the union slate ──────────────
    names = set(candidates) | set(held)
    kelly_fraction = float(_cfg(gov_cfg, "kelly_fraction"))
    mu_shrinkage = float(_cfg(gov_cfg, "mu_shrinkage"))
    mu_by_name: dict[str, float | None] = {}
    sigma_by_name: dict[str, float | None] = {}
    for t in names:
        obj = candidates.get(t) if t in candidates else held.get(t)
        mu_by_name[t] = getattr(obj, "mu", None)
        sigma_by_name[t] = getattr(obj, "sigma", None)

    def _has_moments(t: str) -> bool:
        m, s = _finite(mu_by_name.get(t)), _finite(sigma_by_name.get(t))
        return m is not None and s is not None and s > 0

    # Model fault ≠ weak slate: a non-empty slate where NO name carries
    # usable (μ̂, σ̂) moments means the model layer failed its contract.
    model_fault = bool(names) and not any(_has_moments(t) for t in names)

    raws = {
        t: shrunk_kelly_raw(
            mu_by_name.get(t), sigma_by_name.get(t),
            kelly_fraction=kelly_fraction, mu_shrinkage=mu_shrinkage,
        )
        for t in names
    }
    caps = {t: cap_pct for t in names}

    decision = compute_session_target_exposure(
        raws=raws,
        caps=caps,
        regime=regime,
        e_ceil_by_regime=_cfg(gov_cfg, "e_ceil_by_regime") or {},
        current_gross_exposure=e_current,
        hysteresis_band=float(_cfg(gov_cfg, "hysteresis_band")),
        confidence=getattr(ctx, "confidence", None),
        top_k=int(_cfg(gov_cfg, "top_k")),
        max_step_per_session=float(_cfg(gov_cfg, "max_step_per_session")),
        model_fault=model_fault,
        mu=mu_by_name,
    )
    if decision is None:
        return _fault(ctx, "model_fault" if model_fault else "unmapped_regime")

    ctx.counters["governor_sessions"] = ctx.counters.get("governor_sessions", 0) + 1
    # From here on every path returns True: the Governor owns ALL sizing
    # this session — downstream sizing tasks (TopUpHeldTask/TrimHeldTask)
    # check this via governor_owns_sizing() and NO-OP. Never set on the
    # fault paths above, so fault-fallback keeps full legacy semantics.
    ctx._governor_owns_sizing = True  # noqa: SLF001

    # ── Hysteresis hold: E* = E_current ⇒ NO reallocation this session ─
    if decision.hysteresis_held:
        for ticker in candidates:
            _block(ctx, ticker, "governor_hysteresis_hold")
        ctx.counters["governor_hysteresis_holds"] = (
            ctx.counters.get("governor_hysteresis_holds", 0) + 1
        )
        _stamp_ledger(ctx, decision, e_final=e_current, residual=0.0,
                      binding={"hysteresis_hold": True},
                      e_executed=e_current, integer_residual=0.0)
        log.info(
            "governor_sizing: HYSTERESIS HOLD — E*=%.3f within band of "
            "E_current=%.3f; no reallocation", decision.e_target, e_current,
        )
        return True

    # ── Masks (RFC §1.3: min-hold + wash-sale enter L2 as masks) ──────
    wash_days = int(config.get("wash_sale_days", 0))
    min_hold_days = int(config.get("min_hold_days", 0))
    no_buy: set[str] = set()
    no_sell: set[str] = set()
    from renquant_pipeline.kernel.asset_class import (  # noqa: PLC0415
        resolve_asset_class,
        resolve_validated_crypto_spot_pairs,
        wash_sale_applies_for_ticker,
    )
    from renquant_pipeline.kernel.selection import is_wash_sale_blocked  # noqa: PLC0415
    gov_asset_class = resolve_asset_class(config)
    gov_validated_crypto_pairs = resolve_validated_crypto_spot_pairs(config)
    for ticker, hs in held.items():
        if is_wash_sale_blocked(ticker, ctx.today, ctx.last_sell_dates or {},
                                wash_days,
                                asset_class=gov_asset_class,
                                validated_crypto_pairs=gov_validated_crypto_pairs):
            no_buy.add(ticker)
        entry_date = _coerce_date(getattr(hs, "entry_date", None))
        days_held = (ctx.today - entry_date).days if entry_date else None
        if days_held is not None and days_held < min_hold_days:
            no_sell.add(ticker)
            continue
        # §1091 no-sell guard: selling a LOSS lot bought inside the
        # wash-sale window would realize a disallowed loss. Ticker-scoped
        # (P5 hardening, pipeline#183): a validated crypto spot pair is
        # never subject to §1091, so this guard must not fire for it.
        price = _finite(prices.get(ticker)) or 0.0
        entry_price = _entry_price(hs)
        if (entry_price is not None and price < entry_price
                and days_held is not None and days_held < wash_days
                and wash_sale_applies_for_ticker(gov_asset_class, ticker,
                                                  gov_validated_crypto_pairs)):
            no_sell.add(ticker)

    alloc = allocate_down_only(
        raws=raws,
        caps=caps,
        e_star=decision.e_target,
        top_k=int(_cfg(gov_cfg, "top_k")),
        current_weights=current_w,
        no_buy=no_buy,
        no_sell=no_sell,
        sector_by_name=config.get("sector_map", {}) or {},
        sector_caps=_sector_caps(config, regime_p),
        corr_pair_caps=_corr_pair_caps(ctx, names, cap_pct),
    )

    _execute_deltas(ctx, gov_cfg, decision, alloc, candidates, held,
                    current_w, raws, mu_by_name, sigma_by_name, caps, pv,
                    no_sell)
    return True


# ═════════════════════════════════════════════════════════════════════
#  L3 — integer-aware execution of the weight deltas (RFC §2.3)
# ═════════════════════════════════════════════════════════════════════


def _execute_deltas(ctx, gov_cfg, decision, alloc, candidates, held,
                    current_w, raws, mu_by_name, sigma_by_name, caps, pv,
                    no_sell) -> None:
    config = ctx.config or {}
    prices = ctx.prices or {}
    targets = alloc.weights
    remaining_cash = float(getattr(ctx, "cash", 0.0) or 0.0)
    starting_cash = remaining_cash

    # Conviction order = shrunk-Kelly raw desc (deterministic tiebreak).
    buy_names = [
        t for t in sorted(targets, key=lambda n: (-raws.get(n, 0.0), n))
        if targets.get(t, 0.0) - current_w.get(t, 0.0) > _EPS
    ]
    realized_w = {t: current_w.get(t, 0.0) for t in buy_names}
    bought_shares: dict[str, int] = {t: 0 for t in buy_names}

    def _fill_buys() -> None:
        """Greedy whole-share main pass + residual-cash re-offer sweeps."""
        nonlocal remaining_cash
        # Main pass: floor of the remaining delta, conviction order,
        # cash-aware (a partial fill never overdraws remaining cash and a
        # later, lower-conviction name only sees what is genuinely left).
        for t in buy_names:
            price = _finite(prices.get(t))
            if price is None or price <= 0:
                continue
            need_w = targets[t] - realized_w[t]
            if need_w <= _EPS:
                continue
            shares = min(int(need_w * pv / price),
                         int((remaining_cash + 1e-6) / price))
            cost = shares * price
            if shares >= 1:
                bought_shares[t] += shares
                realized_w[t] += cost / pv
                remaining_cash -= cost
        # Residual pass: re-offer leftover cash one share at a time in
        # conviction order (generalized one-share deferred rescue, S6
        # A-3): a name still short of target may round UP past it by at
        # most one share, bounded by its hard cap — leftover cash goes to
        # the next-highest-conviction affordable name first, so a rescue
        # can never crowd out a higher-conviction candidate's funding.
        progressed = True
        while progressed:
            progressed = False
            for t in buy_names:
                price = _finite(prices.get(t))
                if price is None or price <= 0:
                    continue
                if realized_w[t] >= targets[t] - _EPS:
                    continue
                if price > remaining_cash + 1e-6:
                    continue
                if realized_w[t] + price / pv > caps.get(t, math.inf) + 1e-6:
                    continue
                bought_shares[t] += 1
                realized_w[t] += price / pv
                remaining_cash -= price
                ctx.counters["governor_residual_reoffers"] = (
                    ctx.counters.get("governor_residual_reoffers", 0) + 1
                )
                progressed = True

    _fill_buys()

    # ── Exit legs from weight deltas (RFC §1.3 / §2.3): an exit+entry
    #    pair is emitted only when the post-cost improvement is positive.
    #    Pure de-risking liquidations are NOT emitted here — all existing
    #    exit logic (stops, panel exit, regime halts) is untouched. ─────
    tax_cfg = config.get("tax", {}) or {}
    st_rate = float(tax_cfg.get("short_term_rate", 0.50))
    lt_rate = float(tax_cfg.get("long_term_rate", 0.32))
    lt_threshold = int(tax_cfg.get("long_term_threshold_days", 365))
    txn_cost = float((config.get("rotation", {}) or {}).get(
        "transaction_cost_pct", _DEFAULT_TXN_COST_PER_SIDE * 2)) / 2.0

    def _unfilled_demand() -> float:
        return sum(max(targets[t] - realized_w[t], 0.0) for t in buy_names) * pv

    sell_names = [
        t for t in sorted(held, key=lambda n: (raws.get(n, 0.0), n))
        if t not in no_sell
        and current_w.get(t, 0.0) - targets.get(t, 0.0) > _EPS
    ]
    from renquant_pipeline.kernel.exits import ExitSignal  # noqa: PLC0415
    from renquant_pipeline.kernel.rotation import tax_drag  # noqa: PLC0415
    pairs_emitted = 0
    sold_shares: dict[str, int] = {}
    for t in sell_names:
        demand = _unfilled_demand()
        if demand <= _EPS:
            break
        price = _finite(prices.get(t))
        if price is None or price <= 0:
            continue
        hs = held[t]
        held_shares = _finite(getattr(hs, "shares", 0.0)) or 0.0
        target_w = targets.get(t, 0.0)
        if target_w <= _EPS:
            sell_shares = int(held_shares)          # full liquidation
        else:
            sell_shares = int((current_w[t] - target_w) * pv / price)
        sell_shares = min(sell_shares, int(held_shares))
        if sell_shares < 1:
            continue
        proceeds = sell_shares * price
        held_mu = _finite(getattr(hs, "mu", None))
        if held_mu is None:
            # Unknown μ̂ on the held side ⇒ the improvement is
            # unmeasurable — keep the position (conservative).
            continue
        buy_mu = max(
            (m for m in (
                _finite(mu_by_name.get(b)) for b in buy_names
                if targets[b] - realized_w[b] > _EPS
            ) if m is not None),
            default=None,
        )
        if buy_mu is None:
            continue
        entry_price = _entry_price(hs)
        pnl_pct = ((price - entry_price) / entry_price
                   if entry_price and entry_price > 0 else 0.0)
        entry_date = _coerce_date(getattr(hs, "entry_date", None))
        hold_days = (ctx.today - entry_date).days if entry_date else 0
        tax = tax_drag(pnl_pct, hold_days, st_rate, lt_rate,
                       lt_threshold) * proceeds
        cost = txn_cost * proceeds * 2.0            # exit leg + entry leg
        improvement = (buy_mu - held_mu) * proceeds - tax - cost
        if improvement <= 0.0:
            ctx.counters["governor_pair_rejected_post_cost"] = (
                ctx.counters.get("governor_pair_rejected_post_cost", 0) + 1
            )
            log.info(
                "governor_sizing: pair sell %s REJECTED — post-cost "
                "improvement %.2f ≤ 0 (tax=%.2f cost=%.2f)",
                t, improvement, tax, cost,
            )
            continue
        sig = ExitSignal(
            should_exit=True,
            reason=(f"governor rebalance target={target_w:.1%} "
                    f"current={current_w[t]:.1%} improvement=${improvement:.0f}"),
            exit_type="governor_rebalance",
            quantity=float(sell_shares),
        )
        ctx.exits.append((t, sig))
        pairs_emitted += 1
        remaining_cash += proceeds
        sold_shares[t] = sold_shares.get(t, 0) + sell_shares
        ctx.counters["governor_pair_sells"] = (
            ctx.counters.get("governor_pair_sells", 0) + 1
        )
        log.info(
            "governor_sizing: %s PAIR SELL %d shares @ %.2f ($%.0f, "
            "tax=$%.2f cost=$%.2f improvement=$%.2f) funding buys",
            t, sell_shares, price, proceeds, tax, cost, improvement,
        )
        _fill_buys()                                 # redeploy proceeds

    # ── Emit aggregated buy orders (one per name, conviction order) ───
    orders_emitted = 0
    for t in buy_names:
        shares = bought_shares[t]
        if shares < 1:
            if t in candidates:
                _block(ctx, t, "governor_unfilled_target")
            continue
        price = _finite(prices.get(t)) or 0.0
        invest = shares * price
        c = candidates.get(t)
        hs = held.get(t)
        src = c if c is not None else hs
        order = stamp_order_attribution({
            "ticker": t,
            "shares": shares,
            "price": price,
            "invest": invest,
            "target_pct": invest / pv,
            "regime": ctx.regime,
            "confidence": ctx.confidence,
            "conviction": raws.get(t),
            "sigma_mult": None,
            "rank_score": getattr(src, "rank_score", None),
            "rs_score": getattr(src, "rs_score", None),
            "panel_score": getattr(src, "panel_score", None),
            "sigma": sigma_by_name.get(t),
            "mu": mu_by_name.get(t),
            "kelly_target_pct": getattr(src, "kelly_target_pct", None),
            "detail": getattr(src, "detail", "") or "",
            "order_type": "NEW_BUY" if hs is None else "TOP_UP",
            "sizing_mode": "deployment_governor",
            "target_notional": targets[t] * pv,
            "realized_notional_planned": invest,
        }, ctx=ctx, source_job="SelectionJob",
            source_task="SizeAndEmitTask",
            acceptance_reason="deployment_governor_target",
            source_obj=src,
            decision_inputs={
                "governor_e_target": decision.e_target,
                "governor_e_raw": decision.e_raw,
                "governor_e_ceil": decision.e_ceil,
                "governor_e_current": decision.e_current,
                "allocator_e_final": alloc.e_final,
                "allocator_residual": alloc.residual,
                "target_weight": targets[t],
                "shrunk_kelly_raw": raws.get(t),
                "starting_cash": starting_cash,
            })
        ctx.orders.append(order)
        orders_emitted += 1
        log.info(
            "governor_sizing: %s %s %d shares @ %.2f ($%.0f, target_w=%.2f%%)",
            t, order["order_type"], shares, price, invest, targets[t] * 100,
        )

    # ── L3 executed-state invariant (RFC §2.3): E_executed = the ACTUAL
    #    post-fill/post-sell exposure from realized whole-share quantities,
    #    distinct from L2's continuous E_final. integer_residual is the
    #    gap the whole-share rounding (never the L2 allocator) is
    #    responsible for. ─────────────────────────────────────────────
    executed_w = dict(current_w)
    for t in buy_names:
        executed_w[t] = realized_w[t]
    for t, shares in sold_shares.items():
        price = _finite(prices.get(t)) or 0.0
        executed_w[t] = max(current_w.get(t, 0.0) - shares * price / pv, 0.0)
    e_executed = float(sum(executed_w.values()))
    integer_residual = float(alloc.e_final - e_executed)

    _stamp_ledger(ctx, decision, e_final=alloc.e_final,
                  residual=alloc.residual,
                  binding=alloc.binding_constraints,
                  e_executed=e_executed,
                  integer_residual=integer_residual)
    spent = starting_cash - remaining_cash
    log.info(
        "governor_sizing: %d buy order(s), %d pair sell(s) "
        "(E*=%.3f E_final=%.3f E_executed=%.3f integer_residual=%.3f "
        "spent=$%.0f/$%.0f)",
        orders_emitted, pairs_emitted, decision.e_target, alloc.e_final,
        e_executed, integer_residual, spent, starting_cash,
    )


# ═════════════════════════════════════════════════════════════════════
#  Wiring helpers
# ═════════════════════════════════════════════════════════════════════


def _fault(ctx, reason: str) -> bool:
    """Record a fail-closed Governor fault; caller falls back to legacy."""
    ctx.counters["governor_fault_fallback_legacy"] = (
        ctx.counters.get("governor_fault_fallback_legacy", 0) + 1
    )
    log.warning(
        "governor_sizing: FAULT (%s) — Governor emits no target; "
        "falling back to the legacy sizing path", reason,
    )
    ctx._deployment_governor = {"fault": reason}  # noqa: SLF001
    return False


def _stamp_ledger(ctx, decision, *, e_final: float, residual: float,
                  binding: dict, e_executed: float, integer_residual: float,
                  ) -> None:
    """Decision-ledger payload (RFC §2.1 weak-slate auditability).

    Three auditable numbers, one per layer (RFC §2.3): ``e_target`` (L1's
    E*), ``e_final``/``residual`` (L2's continuous declared exposure),
    ``e_executed``/``integer_residual`` (L3's ACTUAL post-fill exposure
    from realized whole-share quantities, and the whole-share rounding
    gap — distinct from L2's continuous residual).
    """
    ctx._deployment_governor = {  # noqa: SLF001
        "e_target": decision.e_target,
        "e_raw": decision.e_raw,
        "e_ceil": decision.e_ceil,
        "e_current": decision.e_current,
        "hysteresis_held": decision.hysteresis_held,
        "step_limited": decision.step_limited,
        "ceiling_bound": decision.ceiling_bound,
        "l1_candidate": decision.l1_candidate,
        "e_vol": decision.e_vol,
        "slate_stats": decision.slate_stats,
        "e_final": e_final,
        "residual": residual,
        "binding_constraints": binding,
        "e_executed": e_executed,
        "integer_residual": integer_residual,
    }


def _entry_price(hs) -> float | None:
    try:
        wa = hs.weighted_avg_entry_price()
        wa = _finite(wa)
        if wa is not None and wa > 0:
            return wa
    except (AttributeError, TypeError):
        pass
    ep = _finite(getattr(hs, "entry_price", None))
    return ep if ep is not None and ep > 0 else None


def _sector_caps(config: dict, regime_p: dict) -> dict | None:
    """Resolve the sector weight cap with the QP's precedence:
    ``regime_params.<regime>.max_sector_weight_pct`` >
    ``config.max_sector_weight_pct``. None ⇒ unconstrained."""
    cap = regime_p.get("max_sector_weight_pct")
    if cap is None:
        cap = config.get("max_sector_weight_pct")
    cap_f = _finite(cap)
    if cap_f is None or cap_f <= 0:
        return None
    sectors = {s for s in (config.get("sector_map", {}) or {}).values() if s}
    return {s: cap_f for s in sectors}


def _corr_pair_caps(ctx, names: set, cap_pct: float) -> list:
    """High-correlation pair caps: ``w_i + w_j ≤ 2 × per-name cap`` for
    pairs with |ρ| ≥ correlation_guard_threshold — the same convex
    convention as the QP's BuildCorrelationGroupConstraintTask."""
    corr_matrix = getattr(ctx, "corr_matrix", None)
    if not corr_matrix:
        return []
    thr = _finite(((ctx.config or {}).get("regime", {}) or {}).get(
        "correlation_guard_threshold", 0.70))
    if thr is None or thr <= 0.0 or thr >= 1.0:
        return []
    pair_cap = 2.0 * cap_pct
    ordered = sorted(names)
    pairs: list[tuple[str, str, float]] = []
    for i, a in enumerate(ordered):
        row = corr_matrix.get(a) if isinstance(corr_matrix, dict) else None
        for b in ordered[i + 1:]:
            rho = None
            if isinstance(row, dict):
                rho = row.get(b)
            if rho is None and isinstance(corr_matrix, dict):
                other = corr_matrix.get(b)
                if isinstance(other, dict):
                    rho = other.get(a)
            rho_f = _finite(rho)
            if rho_f is None:
                continue
            if abs(rho_f) >= thr:
                pairs.append((a, b, pair_cap))
    return pairs
