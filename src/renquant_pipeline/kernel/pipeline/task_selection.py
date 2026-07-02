"""Selection tasks: prepare context → run greedy loop → size and emit orders."""
from __future__ import annotations

import logging
import math
from typing import Any

from .context import InferenceContext
from .order_attribution import stamp_order_attribution
from .pipeline import Task
from .signal_direction import (
    long_signal_ok_for_object,
    require_positive_raw_signal_cfg as _require_positive_raw_signal_cfg,
)

log = logging.getLogger("kernel.pipeline.selection")


class PrepareSelectionTask(Task):
    """Compute open slots, apply BEAR cap, build SelectionContext → ctx._sel_ctx."""

    def run(self, ctx: InferenceContext) -> bool | None:
        from renquant_pipeline.kernel.selection import SelectionContext  # noqa: PLC0415

        config         = ctx.config
        regime_cfg     = config.get("regime", {})
        regime_params  = config.get("regime_params", {}).get(ctx.regime, {})
        max_positions  = int(regime_params.get(
            "max_concurrent_positions",
            config.get("max_concurrent_positions", 8),
        ))
        wash_days      = int(config.get("wash_sale_days", 0))
        earnings_buf   = int(regime_cfg.get("earnings_buffer_days", 3))
        corr_threshold = float(regime_cfg.get("correlation_guard_threshold", 0.70))
        max_per_sector = int(config.get("max_positions_per_sector", 0))
        sector_map     = config.get("sector_map", {})
        defensive_set  = set(config.get("defensive_tickers", []))
        tiered         = config.get("tiered_thresholds", [])

        # Account for rotations already emitted by RotationJob: the sells will
        # be liquidated this bar (so they don't count as held for guards) and
        # the buys are already booked (so they do count as held for guards).
        rotation_sells = {p.sell_ticker for p in (ctx.rotations or [])}
        rotation_buys  = {p.buy_ticker  for p in (ctx.rotations or [])}
        effective_held = (set(ctx.holdings.keys()) - rotation_sells) | rotation_buys

        held       = list(effective_held)
        open_slots = max_positions - len(held)

        if open_slots <= 0:
            log.info("PrepareSelectionTask: no open slots")
            if ApplyBearDefensiveSleeveTask.is_enabled(ctx):
                ctx._sel_ctx = None  # noqa: SLF001
                return True
            return False

        if ctx.bear_only:
            bear_slots     = int(config.get("bear_defensive_slots", 1))
            defensive_held = sum(1 for t in held if t in defensive_set)
            remaining      = max(bear_slots - defensive_held, 0)
            open_slots     = min(open_slots, remaining)
            if open_slots <= 0:
                log.info("PrepareSelectionTask: no BEAR defensive alpha slots")
                if ApplyBearDefensiveSleeveTask.is_enabled(ctx):
                    ctx._sel_ctx = None  # noqa: SLF001
                    return True
                return False

        ctx._sel_ctx = SelectionContext(  # noqa: SLF001
            today             = ctx.today,
            held_tickers      = held,
            last_sell_dates   = ctx.last_sell_dates,
            # 2026-05-09 audit FIX-A: propagate cost-aware wash-sale data.
            # Pre-fix run_selection_loop used binary block; now uses
            # is_wash_sale_blocked_with_cost (single source of truth).
            last_sell_pls     = getattr(ctx, "last_sell_pls", {}) or {},
            earnings_calendar = ctx.earnings_calendar or {},
            corr_matrix       = ctx.corr_matrix,
            sector_map        = sector_map,
            defensive_set     = defensive_set,
            wash_sale_days    = wash_days,
            earnings_buffer   = earnings_buf,
            corr_threshold    = corr_threshold,
            max_per_sector    = max_per_sector,
            tiered_thresholds = tiered,
            open_slots        = open_slots,
            bear_only         = bool(ctx.bear_only),
        )


class RunSelectionTask(Task):
    """Run the greedy selection loop → ctx._selected, ctx._blocks; update counters.

    Also populates ctx._blocked_by_ticker (Plan P): per-ticker rejection
    reason, fed to candidate_scores.blocked_by in the decision-trace DB.
    """

    def run(self, ctx: InferenceContext) -> bool | None:
        from renquant_pipeline.kernel.selection import run_selection_loop  # noqa: PLC0415

        sel_ctx = getattr(ctx, "_sel_ctx", None)
        if sel_ctx is None:
            ctx._selected = []  # noqa: SLF001
            ctx._blocks = {}  # noqa: SLF001
            return True

        blocked_by_ticker = getattr(ctx, "_blocked_by_ticker", None)
        if blocked_by_ticker is None:
            blocked_by_ticker = {}
        selected, blocks = run_selection_loop(
            ctx.ranked, sel_ctx,
            blocked_by_ticker=blocked_by_ticker,
        )
        ctx._selected          = selected            # noqa: SLF001
        ctx._blocks            = blocks              # noqa: SLF001
        ctx._blocked_by_ticker = blocked_by_ticker   # noqa: SLF001

        ctx.counters["blocked_wash"]  = ctx.counters.get("blocked_wash",  0) + blocks.get("wash_sale",   0)
        ctx.counters["sector_blocks"] = ctx.counters.get("sector_blocks", 0) + blocks.get("sector",      0)
        ctx.counters["corr_blocks"]   = ctx.counters.get("corr_blocks",   0) + blocks.get("correlation", 0)
        # Plan O — non-BEAR defensive rejections (e.g. XLU in BULL_VOLATILE).
        ctx.counters["defensive_non_bear_blocks"] = (
            ctx.counters.get("defensive_non_bear_blocks", 0)
            + blocks.get("defensive_non_bear", 0)
        )


class SizeAndEmitTask(Task):
    """Size each selected ticker and emit buy orders → ctx.orders."""

    def run(self, ctx: InferenceContext) -> bool | None:
        def _block(ticker: str, reason: str) -> None:
            blocked_map = getattr(ctx, "_blocked_by_ticker", None)
            if blocked_map is None:
                blocked_map = {}
                ctx._blocked_by_ticker = blocked_map  # noqa: SLF001
            blocked_map.setdefault(ticker, reason)
            key = f"selection_{reason.split(':', 1)[0]}"
            ctx.counters[key] = ctx.counters.get(key, 0) + 1

        buy_blocked = bool(getattr(ctx, "buy_blocked", False)) and not bool(getattr(ctx, "bear_only", False))
        skip_buys = bool(getattr(ctx, "skip_buys", False))
        if buy_blocked or skip_buys:
            reason = "buy_blocked" if buy_blocked else "skip_buys"
            selected = list(getattr(ctx, "_selected", []) or [])  # noqa: SLF001
            for ticker in selected:
                _block(ticker, reason)
            log.info(
                "SizeAndEmitTask: %s — suppressed %d selected buy(s)",
                reason, len(selected),
            )
            return False

        from renquant_pipeline.kernel.sizing import (  # noqa: PLC0415
            compute_position_size,
            conviction_score_for_object,
            conviction_score_percentiles,
            conviction_multiplier,
            sigma_multiplier,
            universe_sigma_median,
        )

        # Audit fix CONF-MULT (2026-04-25): use floored confidence multiplier
        # so low confidence (e.g. 0.0041 from a Hurst/GMM disagreement) doesn't
        # collapse position size to ~$0. See kernel/regime.py::confidence_to_size_multiplier.
        from renquant_pipeline.kernel.regime import confidence_to_size_multiplier  # noqa: PLC0415
        _conf_mult    = confidence_to_size_multiplier(ctx.confidence)
        regime_p      = ctx.config.get("regime_params", {}).get(ctx.regime, {})
        base_max_pct  = float(regime_p.get("max_position_pct", 0.15)) * _conf_mult

        # CUSUM-v2 Design C (user-locked 2026-04-24): when
        # `regime.cusum_cooldown_mode == "wall_time"`, scale max_pct by
        # cooldown_progress (0→1 over cusum_cooldown_days). Default mode
        # "bar_count" preserves v4 behaviour (hard transition block via
        # TransitionWindowTask; this path is a no-op).
        cooldown_mult = 1.0
        _regime_cfg = ctx.config.get("regime", {})
        if str(_regime_cfg.get("cusum_cooldown_mode", "bar_count")) == "wall_time":
            from renquant_pipeline.kernel.regime import cusum_cooldown_progress  # noqa: PLC0415
            cd_start = getattr(ctx.regime_state, "cooldown_start", None) \
                       if ctx.regime_state is not None else None
            cd_days  = float(_regime_cfg.get("cusum_cooldown_days", 3.0))
            cooldown_mult = cusum_cooldown_progress(ctx.today, cd_start, cd_days)
            if cooldown_mult < 1.0:
                log.info("SizeAndEmitTask: CUSUM cooldown active — "
                         "scaling max_pct × %.3f", cooldown_mult)
        base_max_pct *= cooldown_mult
        reserve_pct   = float(regime_p.get("cash_reserve_pct", 0.0))  * _conf_mult
        bear_def_pct  = float(ctx.config.get("bear_defensive_pct", 0.15))
        bear_def_slots = max(int(ctx.config.get("bear_defensive_slots", 1)), 1)
        override_pct  = (bear_def_pct / bear_def_slots) if ctx.bear_only else None
        sizing_cfg    = (ctx.config.get("ranking", {})
                          .get("panel_scoring", {}).get("sizing", {}))
        sigma_cfg     = (ctx.config.get("ranking", {})
                          .get("panel_scoring", {})
                          .get("sigma_sizing", {}))
        kelly_cfg     = ctx.config.get("ranking", {}).get("kelly_sizing", {})
        kelly_on      = bool(kelly_cfg.get("enabled", False))
        # When Kelly is primary sizer, conviction_multiplier (derived from
        # panel_score) and sigma_multiplier (inverse of σ) approximately
        # re-scale the SAME quantities Kelly already encodes (μ and σ²).
        # Flag lets us test the pure-Kelly hypothesis — no stacked
        # multipliers. Default False preserves v4 behaviour.
        kelly_pure    = bool(kelly_cfg.get("disable_extra_multipliers", False))

        # S6 A-3 (2026-07-02, capability program §1.2 / RS-2 lane-A memo):
        # one-share floor for high-price INITIATIONS. The multiplicative
        # sizing stack (Kelly × conviction × σ-mult) can compound a target
        # notional below ONE share of a high-price name (2026-07-01 OXY
        # forensics: BLK target $324 < 1 share ~$1.1k → size_insufficient_cash
        # → selection drifts toward LOW-price names). When enabled, a
        # floor-clearing candidate that zeroes out ONLY because of whole-share
        # rounding may round UP to exactly one share iff (a) one share ≤
        # regime max_position_pct × PV, and (b) one share ≤ investable
        # headroom after cash reservations. SIZING only — every admission
        # gate (greedy loop, signal-direction, bear caps) has already run by
        # the time this fires. Default OFF; inert until strategy-104 defines
        # `sizing.one_share_floor_enabled: true`. QP already has the analog
        # for HELD names (portfolio_qp qp_min_share_floor_pct, 2026-05-17);
        # this extends the concept to the initiation path.
        _sizing_root = ctx.config.get("sizing")
        one_share_floor_on = (
            bool(_sizing_root.get("one_share_floor_enabled", False))
            if isinstance(_sizing_root, dict) else False
        )

        # Universe σ median over all ranked candidates (σ written by ApplyNGBoostTask).
        sigma_median = universe_sigma_median(
            [getattr(c, "sigma", None) for c in ctx.ranked]
        )
        conviction_scores = conviction_score_percentiles(ctx.ranked)

        # Audit fix SE-1 (Round 2 deep audit, 2026-04-25): pre-fix,
        # `if price is None or price <= 0` let NaN slip through (NaN<=0
        # is False), then `int(invest / NaN_price)` propagated NaN into
        # share counts and order dicts. Fail-SAFE: treat non-finite price
        # the same as None — skip the ticker, log a warning so operators
        # see WHICH ticker had bad data.
        import math as _math
        # Cash-aware portfolio fill (2026-05-01 trade-audit response):
        # 4/28 incident — the system emitted 6 buys × ~$8k each against a
        # ~$10k account (≈5x implied leverage) because each call to
        # compute_position_size saw the SAME ctx.cash constant. Pre-fix
        # was per-position cash check; post-fix tracks `remaining_cash`
        # decremented after each order so the cumulative invest never
        # exceeds available cash. Selection is already ranked by score
        # so first orders are highest conviction; subsequent low-conviction
        # orders simply hit zero cash and skip.
        # Invariant: sum(o.invest for o in ctx.orders emitted here)
        # ≤ ctx.cash - reserve_pct * portfolio_value.
        remaining_cash = float(getattr(ctx, "cash", 0.0) or 0.0)
        starting_cash  = remaining_cash
        for ticker in ctx._selected:  # noqa: SLF001
            price = ctx.prices.get(ticker)
            if price is None or not _math.isfinite(price) or price <= 0:
                log.warning("SizeAndEmitTask: bad price (%s) for %s — skipping",
                            price, ticker)
                _block(ticker, "size_bad_price")
                continue

            c = next((c for c in ctx.ranked if c.ticker == ticker), None)

            # SIGNAL-DIRECTION GATE (2026-06-10): never open a long on a name
            # the model scores BEARISH. A calibrator can map a negative raw
            # panel_score to a positive expected-return by extrapolation; on
            # 2026-06-10 that bought 5 names whose panel_score was −0.10..−0.13
            # while the calibrated μ read +0.034..+0.042. Buying what the model
            # is short is the failure the operator flagged. This is the correct
            # fix for the anti-pattern of setting min_panel_score=null so a
            # whole-universe-negative model can still trade: if every raw score
            # is negative (model bug or genuine universe-bearish), NO new long
            # is admitted — the book holds/sells, it does not long bearish
            # signals. Opt-out only via explicit config (default ON).
            signal_ok, signal_reason = long_signal_ok_for_object(c, ctx.config)
            if not signal_ok:
                log.info(
                    "SizeAndEmitTask: %s BLOCKED new long — %s "
                    "(panel_score=%s expected_return=%s)",
                    ticker,
                    signal_reason,
                    getattr(c, "panel_score", None) if c is not None else None,
                    getattr(c, "expected_return", None) if c is not None else None,
                )
                _block(ticker, signal_reason)
                continue

            if kelly_on and kelly_pure:
                # Pure-Kelly mode — neutralise extra multipliers that
                # overlap with Kelly's μ / σ² inputs.
                conv, sig_m = 1.0, 1.0
            else:
                # 2026-05-04 REVERTED Issue 17 fix: switching from raw
                # panel_score → calibrated rank_score WITHOUT retuning
                # the sizing_cfg.{floor,ceiling,min_mult} for the new
                # scale collapsed positions to ~half size, contributing
                # to the v2 -0.33 Sharpe regression. Original raw
                # panel_score path stays — the structural mismatch the
                # original Issue noted is real but the fix needs a
                # paired sizing_cfg retune in the same change.
                conv_score = conviction_score_for_object(c, sizing_cfg, conviction_scores)
                conv = conviction_multiplier(
                    conv_score, sizing_cfg,
                )
                sig_m = sigma_multiplier(
                    getattr(c, "sigma", None) if c else None,
                    sigma_median, sigma_cfg,
                )
            # Plan C: when kelly_sizing.enabled, the target position
            # weight is the Kelly number precomputed by
            # ApplyKellySizingTask (f* = μ/σ², capped at max_pct +
            # max_concentration). Otherwise: legacy max_pct × conv × σ.
            if kelly_on and c is not None and getattr(c, "kelly_target_pct", None) is not None:
                max_pct = float(c.kelly_target_pct) * conv * sig_m
                if max_pct <= 0:
                    log.info("SizeAndEmitTask: %s Kelly=0 — skip", ticker)
                    _block(ticker, "kelly_zero:capped_zero")
                    continue
            else:
                max_pct = base_max_pct * conv * sig_m

            # Multi-entry accumulation (user-requested 2026-04-24):
            # "65% OK, but not from one session — allow model to buy same
            # stock multiple times". When `per_session_buy_cap` is set,
            # cap any ONE order's target fraction at that value even if
            # kelly_target is higher. Over multiple sessions, top-up and
            # new-buy orders can still build up to the full kelly_target
            # via TopUpHeldTask. Default None = unchanged behaviour.
            per_session_cap = kelly_cfg.get("per_session_buy_cap")
            if per_session_cap is not None:
                cap = float(per_session_cap)
                if cap > 0 and max_pct > cap:
                    log.info("SizeAndEmitTask: %s max_pct %.3f capped to "
                              "per_session %.3f (multi-entry mode)",
                              ticker, max_pct, cap)
                    max_pct = cap

            _, shares = compute_position_size(
                ctx.portfolio_value, remaining_cash,
                max_pct, reserve_pct, price,
                override_pct=override_pct,
            )
            if override_pct is not None and shares * price > (override_pct * ctx.portfolio_value) + 1e-6:
                log.info("SizeAndEmitTask: %s exceeds BEAR defensive slot cap — skip", ticker)
                _block(ticker, "bear_defensive_slot_cap")
                continue
            one_share_floor_applied = False
            if shares < 1 and one_share_floor_on and override_pct is None:
                # A-3 eligibility (contract, RS-2 §A-3): round UP to exactly
                # ONE share iff (a) one share fits under the regime's own
                # max_position_pct × PV (the UNSCALED regime cap — the floor
                # is a minimum-investability exception bounded by the hard
                # regime cap, per §1.2 "1 share ≤ min(max_position_pct × PV,
                # available headroom)"), and (b) one share fits inside the
                # investable headroom after the cash reservation — the same
                # reservation compute_position_size applies. The candidate
                # has already passed every admission gate above; this changes
                # sizing only. BEAR defensive slots (override_pct) keep the
                # legacy drop behaviour.
                regime_cap_dollars = (
                    float(regime_p.get("max_position_pct", 0.15))
                    * float(ctx.portfolio_value or 0.0)
                )
                investable = max(
                    remaining_cash - reserve_pct * float(ctx.portfolio_value or 0.0),
                    0.0,
                )
                if price <= regime_cap_dollars + 1e-6 and price <= investable + 1e-6:
                    shares = 1
                    one_share_floor_applied = True
                    ctx.counters["one_share_floor_roundups"] = (
                        ctx.counters.get("one_share_floor_roundups", 0) + 1
                    )
                    log.info(
                        "SizeAndEmitTask: %s ONE_SHARE_FLOOR — target $%.0f "
                        "< 1 share $%.2f; rounding UP to 1 share "
                        "(1 share = %.1f%% PV ≤ regime cap %.1f%%, "
                        "investable=$%.0f)",
                        ticker, max_pct * ctx.portfolio_value, price,
                        100.0 * price / ctx.portfolio_value
                        if ctx.portfolio_value > 0 else 0.0,
                        100.0 * float(regime_p.get("max_position_pct", 0.15)),
                        investable,
                    )
            if shares < 1:
                log.info("SizeAndEmitTask: %s insufficient cash — skip "
                         "(remaining_cash=$%.0f price=$%.2f)",
                         ticker, remaining_cash, price)
                _block(ticker, "size_insufficient_cash")
                continue

            invest     = shares * price
            # Defensive: per-position sizer already rounded down to whole
            # shares within remaining_cash, but assert the invariant —
            # sum of emitted invests must not exceed starting_cash.
            if invest > remaining_cash + 1e-6:  # fp-tolerance
                log.warning(
                    "SizeAndEmitTask: %s invest=$%.0f > remaining_cash=$%.0f "
                    "— skipping to preserve cash invariant",
                    ticker, invest, remaining_cash,
                )
                _block(ticker, "size_cash_invariant")
                continue
            target_pct = invest / ctx.portfolio_value if ctx.portfolio_value > 0 else 0.0
            ctx.orders.append(stamp_order_attribution({
                "ticker":     ticker,
                "shares":     shares,
                "price":      price,
                "invest":     invest,
                "target_pct": target_pct,
                "regime":     ctx.regime,
                "confidence": ctx.confidence,
                "conviction": conv,
                "sigma_mult": sig_m,
                "rank_score": c.rank_score  if c else 0.0,
                "rs_score":   c.rs_score    if c else 0.0,
                "panel_score": getattr(c, "panel_score", None) if c else None,
                "sigma":      getattr(c, "sigma", None)        if c else None,
                "mu":         getattr(c, "mu", None)           if c else None,
                # Thesis-degradation baseline (Approach A) — carry the
                # Kelly target THE MODEL COMPUTED for this candidate so
                # adapters can stamp it as entry_kelly_target_pct. Distinct
                # from `target_pct` (the actually-sized fraction).
                "kelly_target_pct": getattr(c, "kelly_target_pct", None) if c else None,
                "detail":     c.detail      if c else "",
                # Order provenance — distinguished in trade log so audits
                # can tell why a buy fired (NEW_BUY vs TopUp Kelly maintenance
                # vs rotation vs QP). TopUpHeldTask sets "TOP_UP" on its
                # orders; this is the fresh-entry path.
                "order_type": "NEW_BUY",
                # A-3 dedicated ledger reason field: stamped ONLY when the
                # one-share floor actually rounded this order up, so every
                # round-up is auditable in the ledger and flag-off orders
                # stay byte-identical.
                **({"size_floor_reason": "one_share_floor_round_up"}
                   if one_share_floor_applied else {}),
            }, ctx=ctx, source_job="SelectionJob",
                source_task="SizeAndEmitTask",
                acceptance_reason="selected_by_greedy_loop",
                source_obj=c,
                decision_inputs={
                    "max_pct": max_pct,
                    "reserve_pct": reserve_pct,
                    "remaining_cash_before": remaining_cash,
                    "conviction": conv,
                    "sigma_mult": sig_m,
                    "kelly_enabled": kelly_on,
                    **({"one_share_floor_applied": True}
                       if one_share_floor_applied else {}),
                }))
            remaining_cash -= invest
            log.info(
                "SizeAndEmitTask: %s NEW_BUY %d shares @ %.2f "
                "($%.0f, %.1f%% target, conv=%.2f σ_mult=%.2f) "
                "remaining_cash=$%.0f",
                ticker, shares, price, invest, target_pct * 100,
                conv, sig_m, remaining_cash,
            )

        spent = starting_cash - remaining_cash
        log.info(
            "SizeAndEmitTask: %d orders placed (spent=$%.0f / starting_cash=$%.0f)",
            len(ctx.orders), spent, starting_cash,
        )


class ApplyBearDefensiveSleeveTask(Task):
    """Append default-off fixed-slot defensive buys in BEAR without alpha models."""

    SOURCE_TASK = "ApplyBearDefensiveSleeveTask"

    @staticmethod
    def is_enabled(ctx: InferenceContext) -> bool:
        cfg = (getattr(ctx, "config", None) or {}).get("bear_defensive_sleeve", {}) or {}
        return bool(getattr(ctx, "bear_only", False)) and bool(cfg.get("enabled", False))

    def run(self, ctx: InferenceContext) -> bool | None:
        if not self.is_enabled(ctx):
            return True

        cfg = getattr(ctx, "config", None) or {}
        defensive_tickers = [
            str(t).upper()
            for t in (cfg.get("defensive_tickers", []) or [])
            if str(t).strip()
        ]
        if not defensive_tickers:
            log.info("%s: no defensive_tickers configured", self.SOURCE_TASK)
            return True

        slots = self._positive_int(cfg.get("bear_defensive_slots", 1), default=1)
        sleeve_pct = self._positive_float(cfg.get("bear_defensive_pct", 0.15), default=0.15)
        slot_pct = sleeve_pct / slots
        portfolio_value = float(getattr(ctx, "portfolio_value", 0.0) or 0.0)
        if portfolio_value <= 0 or not math.isfinite(portfolio_value):
            log.warning("%s: invalid portfolio_value=%s", self.SOURCE_TASK, portfolio_value)
            return True

        held = {str(t).upper() for t in (getattr(ctx, "holdings", {}) or {}).keys()}
        defensive_set = set(defensive_tickers)
        held_defensive = held & defensive_set
        ordered = self._ordered_tickers(ctx)
        long_ordered = self._long_entry_order_tickers(ctx)
        ordered_defensive = long_ordered & defensive_set
        regime_params = (cfg.get("regime_params", {}) or {}).get(getattr(ctx, "regime", None), {}) or {}
        max_positions = self._positive_int(
            regime_params.get("max_concurrent_positions", cfg.get("max_concurrent_positions", 8)),
            default=8,
        )
        portfolio_open_slots = max(max_positions - len(held) - len(long_ordered), 0)
        defensive_open_slots = max(slots - len(held_defensive) - len(ordered_defensive), 0)
        open_slots = min(portfolio_open_slots, defensive_open_slots)
        if open_slots <= 0:
            log.info("%s: defensive slots full", self.SOURCE_TASK)
            return True

        reserve_pct = self._nonnegative_float(regime_params.get("cash_reserve_pct", 0.0), default=0.0)
        remaining_cash = self._remaining_cash(ctx)
        investable_cash = max(remaining_cash - portfolio_value * reserve_pct, 0.0)
        if investable_cash <= 0:
            log.info("%s: cash reserve leaves no investable cash", self.SOURCE_TASK)
            return True

        emitted = 0
        for ticker in defensive_tickers:
            if emitted >= open_slots:
                break
            if ticker in held or ticker in ordered:
                continue
            price = self._price_for(ctx, ticker)
            if price is None:
                self._block(ctx, ticker, "bear_defensive_bad_price")
                continue

            cap_dollars = min(slot_pct * portfolio_value, investable_cash)
            shares = int(cap_dollars / price)
            if shares < 1:
                self._block(ctx, ticker, "bear_defensive_insufficient_cash")
                continue

            invest = shares * price
            target_pct = invest / portfolio_value
            order = stamp_order_attribution({
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "invest": invest,
                "target_pct": target_pct,
                "regime": getattr(ctx, "regime", None),
                "confidence": getattr(ctx, "confidence", None),
                "rank_score": None,
                "rs_score": None,
                "panel_score": None,
                "sigma": None,
                "mu": None,
                "kelly_target_pct": None,
                "detail": "BEAR defensive sleeve fixed-slot buy",
                "order_type": "BEAR_DEFENSIVE_SLEEVE",
            }, ctx=ctx, source_job="SelectionJob",
                source_task=self.SOURCE_TASK,
                acceptance_reason="bear_defensive_sleeve_enabled",
                decision_inputs={
                    "slot_pct": slot_pct,
                    "sleeve_pct": sleeve_pct,
                    "slots": slots,
                    "open_slots_before": open_slots,
                    "cash_reserve_pct": reserve_pct,
                    "remaining_cash_before": remaining_cash,
                    "investable_cash_before": investable_cash,
                })
            order["order_source"] = "BEAR_DEFENSIVE_SLEEVE"
            order["source"] = "BEAR_DEFENSIVE_SLEEVE"
            order["decision_inputs"]["order_source"] = "BEAR_DEFENSIVE_SLEEVE"
            ctx.orders.append(order)
            emitted += 1
            ordered.add(ticker)
            investable_cash -= invest
            remaining_cash -= invest
            ctx.counters["bear_defensive_sleeve_orders"] = (
                ctx.counters.get("bear_defensive_sleeve_orders", 0) + 1
            )
            log.info(
                "%s: %s %d shares @ %.2f ($%.0f, %.1f%% target)",
                self.SOURCE_TASK, ticker, shares, price, invest, target_pct * 100,
            )

        return True

    @staticmethod
    def _positive_int(value: Any, *, default: int) -> int:
        try:
            out = int(value)
        except (TypeError, ValueError):
            return default
        return out if out > 0 else default

    @staticmethod
    def _positive_float(value: Any, *, default: float) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out if math.isfinite(out) and out > 0 else default

    @staticmethod
    def _nonnegative_float(value: Any, *, default: float) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return default
        return out if math.isfinite(out) and out >= 0 else default

    @staticmethod
    def _ordered_tickers(ctx: InferenceContext) -> set[str]:
        out: set[str] = set()
        for order in getattr(ctx, "orders", []) or []:
            if not isinstance(order, dict):
                continue
            ticker = order.get("ticker")
            if ticker:
                out.add(str(ticker).upper())
        return out

    @staticmethod
    def _long_entry_order_tickers(ctx: InferenceContext) -> set[str]:
        out: set[str] = set()
        for order in getattr(ctx, "orders", []) or []:
            if not isinstance(order, dict):
                continue
            if ApplyBearDefensiveSleeveTask._is_non_long_entry_order(order):
                continue
            ticker = order.get("ticker")
            if ticker:
                out.add(str(ticker).upper())
        return out

    @staticmethod
    def _is_non_long_entry_order(order: dict) -> bool:
        order_type = str(order.get("order_type") or "").upper()
        side = str((order.get("decision_inputs") or {}).get("side") or "").lower()
        return order_type.startswith("BUY_TO_COVER") or side == "buy_to_close"

    @staticmethod
    def _remaining_cash(ctx: InferenceContext) -> float:
        cash = float(getattr(ctx, "cash", 0.0) or 0.0)
        for order in getattr(ctx, "orders", []) or []:
            if not isinstance(order, dict):
                continue
            try:
                cash -= float(order.get("invest", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
        return max(cash, 0.0)

    @staticmethod
    def _price_for(ctx: InferenceContext, ticker: str) -> float | None:
        value = (getattr(ctx, "prices", {}) or {}).get(ticker)
        if value is None:
            value = (getattr(ctx, "prices", {}) or {}).get(ticker.upper())
        price = ApplyBearDefensiveSleeveTask._finite_positive(value)
        if price is not None:
            return price
        frame = (getattr(ctx, "ohlcv", {}) or {}).get(ticker)
        if frame is None:
            frame = (getattr(ctx, "ohlcv", {}) or {}).get(ticker.upper())
        try:
            close = frame["close"].dropna().iloc[-1]
        except Exception:
            return None
        return ApplyBearDefensiveSleeveTask._finite_positive(close)

    @staticmethod
    def _finite_positive(value: Any) -> float | None:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) and out > 0 else None

    @staticmethod
    def _block(ctx: InferenceContext, ticker: str, reason: str) -> None:
        blocked_map = getattr(ctx, "_blocked_by_ticker", None)
        if blocked_map is None:
            blocked_map = {}
            ctx._blocked_by_ticker = blocked_map  # noqa: SLF001
        blocked_map.setdefault(ticker, reason)
        ctx.counters[reason] = ctx.counters.get(reason, 0) + 1
