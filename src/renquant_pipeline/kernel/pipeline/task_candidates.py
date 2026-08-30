"""Per-ticker buy candidate scoring tasks."""
from __future__ import annotations

import logging

from .context import TickerInferenceContext
from .pipeline import Task

log = logging.getLogger("kernel.pipeline.candidates")


def _panel_watchlist_candidate_mode(tc: TickerInferenceContext) -> bool:
    """True when this candidate is intentionally panel-only.

    This is paired with ``pp_inference._panel_watchlist_candidate_mode``.  A
    panel-only candidate has no tournament artifact by design; it remains
    subject to every pre-panel risk/metadata gate and receives its scores from
    ``PanelScoringJob`` later in the same inference run.
    """
    panel_cfg = (
        (tc.config.get("ranking", {}) or {}).get("panel_scoring", {}) or {}
    )
    return (
        bool(panel_cfg.get("enabled", False))
        and bool(panel_cfg.get("bypass_ticker_gate", False))
        and panel_cfg.get("candidate_universe") == "watchlist"
    )


class EarningsFilterTask(Task):
    def run(self, tc: TickerInferenceContext) -> bool | None:
        from renquant_pipeline.kernel.selection import is_earnings_blocked  # noqa: PLC0415
        earnings_buf = int(tc.config.get("regime", {}).get("earnings_buffer_days", 3))
        if is_earnings_blocked(tc.ticker, tc.today, tc.earnings_calendar or {}, earnings_buf):
            tc.blocked_by = "earnings_blackout"
            log.info("DROP_EarningsFilter [%s]: blocked (within ±%dd of earnings)",
                     tc.ticker, earnings_buf)
            return False


class WashSaleFilterTask(Task):
    """Cost-aware wash-sale gate per IRC §1091.

    The 2026-05-09 economic-cost rewrite: §1091 only disallows the loss
    deduction on a sale that REALIZED A LOSS within ±30d of the buy-back.
    Sales at a GAIN have no §1091 cost. Loss sales have only an NPV
    time-value cost (deferred deduction).

    This task runs at the per-ticker pre-screen stage where μ̂ isn't
    available yet, so:
      - GAIN sales pass (no cost)
      - LOSS sales WITHIN window are blocked unless their NPV cost is below
        the materiality floor (pipeline#223/#227: no downstream call site
        ever passes μ̂, so this is the ONLY re-admission a loss sale gets —
        "the post-NGB economic gate can re-admit them" does not happen)
      - sales OUTSIDE window pass (rule doesn't apply)

    Config:
      asset_class          : str — "crypto" is PROPERTY (RFC 2026-07-10 P5),
                             but §1091 is bypassed only when the ticker is
                             ALSO an explicitly validated non-security spot
                             pair (see resolve_validated_crypto_spot_pairs /
                             wash_sale_applies_for_ticker, pipeline#183 P5
                             hardening) — asset_class alone is insufficient.
                             Absent ⇒ "us_equity" ⇒ byte-identical equity
                             behavior.
      wash_sale_days       : int — window in days (default 30)
      wash_sale_tax_rate   : float — combined federal+state rate (0.30)
      wash_sale_discount_rate : float — for NPV (0.05)
      wash_sale_hold_years : float — expected hold of replacement (2.0)
      wash_sale_min_material_npv : float — NPV cost floor below which a loss
                             sale does not block a buy (pipeline#223). Absent
                             ⇒ WASH_SALE_MIN_MATERIAL_NPV_LEGACY (kernel.selection),
                             the same default the other two buy-admission call
                             sites (task_joint_actions.py, task_rotation.py)
                             use when unconfigured.
      risk.wash_sale.materiality_floor_usd / .assumed_marginal_rate — the
                             GOVERNED materiality floor (s104 design
                             2026-08-02; pipeline#223). Absent/0.0 ⇒ the
                             floor path is inert and this task is
                             byte-identical to today. floor > 0 ⇒ an
                             already-blocked LOSS name whose estimated
                             foregone tax benefit (event-net disallowed loss
                             × assumed marginal rate, ceil'd to the cent) is
                             <= the floor is WAIVED per-name with a
                             decision-trace record; an unavailable estimate
                             leaves the block standing, stamped
                             `estimate_unavailable`. Detection logic
                             (`is_wash_sale_blocked_with_cost`) is unchanged.
    """

    def run(self, tc: TickerInferenceContext) -> bool | None:
        from renquant_pipeline.kernel.asset_class import (  # noqa: PLC0415
            resolve_asset_class,
            resolve_validated_crypto_spot_pairs,
        )
        from renquant_pipeline.kernel.selection import (  # noqa: PLC0415
            estimate_foregone_wash_sale_tax_benefit_usd,
            is_wash_sale_blocked_with_cost,
            resolve_wash_sale_materiality_policy,
            resolve_wash_sale_min_material_npv,
        )
        wash_days = int(tc.config.get("wash_sale_days", 0))
        tax_rate = float(tc.config.get("wash_sale_tax_rate", 0.30))
        disc = float(tc.config.get("wash_sale_discount_rate", 0.05))
        hold_yrs = float(tc.config.get("wash_sale_hold_years", 2.0))
        min_material_npv = resolve_wash_sale_min_material_npv(tc.config)
        blocked, reason, cost_npv = is_wash_sale_blocked_with_cost(
            tc.ticker,
            tc.today,
            tc.last_sell_dates or {},
            tc.last_sell_pls or {},
            wash_days,
            tax_rate=tax_rate,
            discount_rate=disc,
            estimated_hold_years=hold_yrs,
            expected_dollar_return=None,   # μ̂ not yet known at this stage
            # pipeline#223/#227: this is the live buy-admission path; a
            # ticker dropped here never reaches task_joint_actions.py or
            # task_rotation.py, so it must opt in too or the fix is a no-op
            # for the path that actually zeroed sessions.
            min_material_npv_cost=min_material_npv,
            asset_class=resolve_asset_class(tc.config or {}),
            validated_crypto_pairs=resolve_validated_crypto_spot_pairs(tc.config or {}),
        )
        # Governed materiality floor (s104 design 2026-08-02; pipeline#223).
        # Resolution is a pure read — output-invariant on every path.
        policy = resolve_wash_sale_materiality_policy(tc.config)
        if policy.findings:
            # Loud, never silent: an invalid configured VALUE disables the
            # floor (nothing waives) AND the findings are recorded on the
            # decision-record surface the run bundle collects
            # (collect_wash_sale_decision_records aggregates + dedupes them).
            tc.wash_sale_floor_findings = policy.finding_records()
        if blocked:
            stamp = ""
            if policy.floor_usd > 0.0:
                # ZERO-FLOOR SHORT-CIRCUIT IS NORMATIVE (s104 design): this
                # branch is entered ONLY when floor > 0. At floor == 0.0 the
                # `estimate <= floor` comparison must NEVER be evaluated —
                # a name whose estimate is exactly $0.00 still blocks.
                est = estimate_foregone_wash_sale_tax_benefit_usd(
                    (tc.last_sell_pls or {}).get(tc.ticker),
                    assumed_marginal_rate=policy.assumed_marginal_rate,
                )
                if est is None:
                    # Fail toward protection: no estimate ⇒ the block STANDS,
                    # stamped so the trace shows the floor was consulted.
                    stamp = " [estimate_unavailable]"
                elif est <= policy.floor_usd:
                    tc.wash_sale_waiver = {
                        "gate": "wash_sale",
                        "ticker": tc.ticker,
                        "waived": True,
                        "est_foregone_tax_usd": est,
                        "floor_usd": policy.floor_usd,
                        "config_fingerprint": policy.config_fingerprint,
                    }
                    log.info(
                        "WAIVE_WashSaleFilter [%s]: est foregone tax $%.2f <= "
                        "floor $%.2f — buy proceeds (block reason was: %s)",
                        tc.ticker, est, policy.floor_usd, reason,
                    )
                    return None
            reason_txt = f"{reason}{stamp}"
            tc.blocked_by = f"wash_sale:{reason_txt}"
            log.info("DROP_WashSaleFilter [%s]: %s", tc.ticker, reason_txt)
            return False
        # Not blocked but log the reason so the audit trail shows
        # whether we passed because of "gain sale" / "outside window" /
        # "no recent sale".
        if reason and "no recent sale" not in reason and "disabled" not in reason:
            log.debug("PASS_WashSaleFilter [%s]: %s (cost_npv=$%.2f)",
                      tc.ticker, reason, cost_npv)


def collect_wash_sale_decision_records(ctx, tctxs) -> None:
    """Aggregate per-name wash-sale waiver records + config findings onto
    ``ctx.wash_sale_decision_records``.

    That attribute is the surface the run-bundle builders collect
    (``inference.runtime_inference_payload`` /
    ``live_context_snapshot_from_live_context`` append it into
    ``decision_trace``, which ``build_native_live_bundle`` requires) — the
    AC6-binding surface named by the s104 design.

    Inert at the default: with floor 0.0/absent and a valid config, no tc
    carries a waiver or finding, the attribute is never created, and every
    downstream byte is unchanged. Config findings are per-run facts stamped
    on every ticker's tc; they are deduped here and logged loudly ONCE.
    """
    records: list[dict] = []
    seen_findings: set[str] = set()
    for tc in tctxs:
        waiver = getattr(tc, "wash_sale_waiver", None)
        if isinstance(waiver, dict):
            records.append(dict(waiver))
        for rec in getattr(tc, "wash_sale_floor_findings", None) or []:
            if not isinstance(rec, dict):
                continue
            key = str(rec.get("finding"))
            if key in seen_findings:
                continue
            seen_findings.add(key)
            log.error(
                "wash-sale materiality floor CONFIG FINDING "
                "(floor DISABLED, nothing waived): %s", key,
            )
            records.append(dict(rec))
    if not records:
        return
    existing = getattr(ctx, "wash_sale_decision_records", None)
    if existing is None:
        existing = []
        ctx.wash_sale_decision_records = existing
    existing.extend(records)


class SectorMapGateTask(Task):
    """Require sector metadata before a ticker can enter buy selection.

    Panel-LTR neutralization, relative strength, and QP sector caps all rely
    on ``sector_map``. A missing sector must not silently degrade to
    ``rs_score=0`` and no sector cap, because that creates unmanaged sector
    bets in live trading.
    """

    def run(self, tc: TickerInferenceContext) -> bool | None:
        require = bool(
            tc.config.get("risk", {}).get(
                "require_sector_map_for_buys",
                tc.config.get("ranking", {})
                         .get("panel_scoring", {})
                         .get("enabled", False),
            )
        )
        if not require:
            return None
        benchmark = tc.config.get("benchmark", "SPY")
        if tc.ticker == benchmark:
            return None
        sector_map = tc.config.get("sector_map", {}) or {}
        sector = sector_map.get(tc.ticker)
        if not isinstance(sector, str) or not sector:
            tc.blocked_by = "missing_sector_map"
            log.info(
                "DROP_SectorMapGate [%s]: missing sector_map entry "
                "(required for RS + QP sector caps)",
                tc.ticker,
            )
            return False


class BuildFeaturesTask(Task):
    def run(self, tc: TickerInferenceContext) -> bool | None:
        # Feature cache optimization (2026-04-24): if SimAdapter pre-built
        # a full-range feature frame for this ticker, slice it up to today
        # instead of rebuilding from OHLCV (10x faster per bar).
        cached = getattr(tc, "feature_cache_frame", None)
        if cached is not None and not cached.empty:
            tc.features = cached.loc[:tc.today]
            if tc.features is None or tc.features.empty:
                tc.blocked_by = "empty_cached_features"
                log.info("DROP_BuildFeatures [%s]: cached frame slice is empty "
                         "for date %s (cache range: %s → %s)",
                         tc.ticker, tc.today,
                         cached.index.min() if len(cached) else "?",
                         cached.index.max() if len(cached) else "?")
                return False
            return None

        from renquant_pipeline.kernel.indicators import build_feature_frame  # noqa: PLC0415
        stock_df = tc.ohlcv.get(tc.ticker)
        spy_df   = tc.ohlcv.get("SPY")
        panel_only = _panel_watchlist_candidate_mode(tc) and tc.model is None
        if stock_df is None or (tc.model is None and not panel_only) or spy_df is None:
            missing = []
            if stock_df is None:
                missing.append("stock_ohlcv")
            if tc.model is None and not panel_only:
                missing.append("model")
            if spy_df is None:
                missing.append("spy_ohlcv")
            tc.blocked_by = "missing_input:" + ",".join(missing)
            log.info("DROP_BuildFeatures [%s]: missing input "
                     "(stock_df=%s, model=%s, spy=%s)",
                     tc.ticker, stock_df is not None,
                     tc.model is not None, spy_df is not None)
            return False
        # Panel-only candidates are scored from the full panel feature frames
        # later in PanelScoringJob.  Building the legacy per-ticker feature
        # frame here is both unnecessary and impossible without a tournament
        # model contract.
        if panel_only:
            return None

        spec    = tc.config.get("indicator_spec", {})
        vol_win = int(tc.config.get("regime", {}).get("vol_realized_window", 20))
        tc.features = build_feature_frame(stock_df, spy_df, spec, vol_win)
        if tc.features is None or tc.features.empty:
            tc.blocked_by = "empty_features"
            log.info("DROP_BuildFeatures [%s]: build_feature_frame returned empty",
                     tc.ticker)
            return False


class ScoreBuyTask(Task):
    """Score ticker with per-ticker tournament model.

    Default: drop if `signal != "buy"` — the tournament model acts as a binary
    admission gate. This was the 103 behavior and is why many watchlists sat
    in cash for extended periods when per-ticker models got conservative.

    When `ranking.panel_scoring.bypass_ticker_gate == true`, the tournament's
    signal/threshold is advisory only: we still compute and record raw/rank
    scores for logging, but do NOT filter on them. Panel-LTR (which is a
    cross-sectional ranker) then gets to see every admissible ticker and
    rank them itself. The downstream `min_model_score` tier + panel
    `buy_floor` + selection-loop tiered thresholds still enforce quality.
    """

    def run(self, tc: TickerInferenceContext) -> bool | None:
        if tc.model is None and _panel_watchlist_candidate_mode(tc):
            # PanelScoringJob overwrites these placeholders with the active
            # panel model's raw score, calibrated rank and expected return.
            #
            # orch#1082 (2026-08-29): the expected return is NOT a 0.0
            # placeholder. A panel-only candidate has no forecast until
            # ApplyGlobalCalibrationTask stamps one WITH its horizon; a
            # candidate dropped before that point (RealizedVolGateTask,
            # panel_score_missing, panel_scorer_load_failed) kept the 0.0
            # and its None horizon all the way into candidate_scores /
            # ticker_daily_state, where the decision-trace validator
            # (persistence.decision_trace_integrity_report) counts
            # ``expected_return IS NOT NULL AND horizon IS NULL`` as a
            # gap and fails the commit. "No forecast" is None; every
            # reader between here and calibration already treats None as
            # absent (rotation/joint-actions coerce via ``or 0.0``, the
            # gates test ``is not None``).
            tc.model_action = "panel_pending"
            tc._raw_score = 0.0  # noqa: SLF001
            tc._rank_score = 0.0  # noqa: SLF001
            tc._expected_return = None  # noqa: SLF001
            tc._expected_return_horizon_days = None  # noqa: SLF001
            return None

        from renquant_pipeline.kernel.models import (  # noqa: PLC0415
            abstain_block_reason, score_artifact,
        )
        rotation_horizon = int(tc.config.get("rotation", {}).get("target_horizon_days", 20))
        sr = score_artifact(
            tc.model, tc.features.iloc[-1],
            holdings=0, horizon_days=rotation_horizon,
        )
        tc.model_action = sr.signal

        if sr.abstained:
            # 2026-08-30: the per-ticker model has NO opinion (unseen
            # Q-state, or NaN / missing required features for any model
            # type — pipeline#303 + follow-up). This is the same shape as the
            # panel-only "no forecast yet" placeholder (pipeline#302):
            # every score is None, never 0.0. Unlike that placeholder
            # nothing downstream will stamp a forecast later, so the
            # candidate is dropped HERE — it is not a buy and not a
            # rotation buy-leg — with a stable reason. This is not the
            # advisory tournament gate: ``bypass_ticker_gate`` bypasses a
            # SIGNAL the model gave; an abstain is the absence of one.
            tc._raw_score       = None   # noqa: SLF001
            tc._rank_score      = None   # noqa: SLF001
            tc._expected_return = None   # noqa: SLF001
            tc._expected_return_horizon_days = None  # noqa: SLF001
            tc.blocked_by = abstain_block_reason(sr.abstain_reason)
            log.info("DROP_ScoreBuy [%s]: model abstained (%s) — no raw score, "
                     "no expected return; not a buy, not a rotation buy-leg",
                     tc.ticker, sr.abstain_reason)
            return False

        log.debug("ScoreBuyTask [%s]: action=%s  raw=%.4f  rank=%.4f  er=%.4f",
                  tc.ticker, sr.signal, sr.raw_score, sr.rank_score, sr.expected_return)

        # Always record scores so downstream tasks + logs have them.
        tc._raw_score       = sr.raw_score          # noqa: SLF001
        tc._rank_score      = sr.rank_score         # noqa: SLF001
        tc._expected_return = sr.expected_return    # noqa: SLF001
        tc._expected_return_horizon_days = rotation_horizon  # noqa: SLF001

        bypass = bool(
            tc.config.get("ranking", {})
                      .get("panel_scoring", {})
                      .get("bypass_ticker_gate", False)
        )
        if bypass:
            return
        if sr.signal != "buy":
            tc.blocked_by = f"model_signal:{sr.signal}"
            log.info("DROP_ScoreBuy [%s]: signal=%s (not 'buy')",
                     tc.ticker, sr.signal)
            return False


class ScoreThresholdTask(Task):
    """Reject candidates whose tournament `rank_score` < regime min_model_score.

    Skipped when `ranking.panel_scoring.bypass_ticker_gate == true` — the
    tournament's calibrated rank_score is an unreliable admission signal
    in sparse-buy regimes; Panel-LTR will overwrite rank_score via
    PanelScoringJob and the selection loop then applies its own tiered
    thresholds on the panel-calibrated score.
    """

    def run(self, tc: TickerInferenceContext) -> bool | None:
        bypass = bool(
            tc.config.get("ranking", {})
                      .get("panel_scoring", {})
                      .get("bypass_ticker_gate", False)
        )
        if bypass:
            return
        # Audit fix TC-1 (Round 2 deep audit, 2026-04-25): pre-fix, NaN
        # rank passed the `< min_score` gate (NaN < x is False) →
        # candidate proceeded with NaN rank_score. Treat NaN as worst
        # (= rejected).
        import math
        min_score = float(tc.regime_params.get("min_model_score", 0.10))
        rank      = getattr(tc, "_rank_score", 0.0)
        if rank is None or not math.isfinite(rank) or rank < min_score:
            tc.blocked_by = "rank_below_min"
            log.info("DROP_ScoreThreshold [%s]: rank=%s < min=%.4f",
                     tc.ticker, rank, min_score)
            return False


class RelativeStrengthTask(Task):
    def run(self, tc: TickerInferenceContext) -> bool | None:
        from renquant_pipeline.kernel.selection import compute_relative_strength  # noqa: PLC0415
        sector_map = tc.config.get("sector_map", {})
        sector_etf = tc.config.get("sector_etf_map", {})
        etf = sector_etf.get(sector_map.get(tc.ticker, "other"))
        if not etf or etf not in tc.ohlcv:
            tc.rs_score = 0.0
            return
        stock_df = tc.ohlcv.get(tc.ticker)
        etf_df   = tc.ohlcv[etf]
        if len(stock_df) >= 21 and len(etf_df) >= 21:
            try:
                stock_r = float(stock_df["close"].iloc[-1] / stock_df["close"].iloc[-21] - 1)
                etf_r   = float(etf_df["close"].iloc[-1]   / etf_df["close"].iloc[-21]   - 1)
                tc.rs_score = compute_relative_strength(stock_r, etf_r)
            except Exception:
                tc.rs_score = 0.0
        else:
            tc.rs_score = 0.0
        log.debug("RelativeStrengthTask [%s]: rs=%.4f", tc.ticker, tc.rs_score)


class AssembleCandidateTask(Task):
    def run(self, tc: TickerInferenceContext) -> bool | None:
        from renquant_pipeline.kernel.selection import CandidateResult  # noqa: PLC0415
        raw  = getattr(tc, "_raw_score",        0.0)
        rank = getattr(tc, "_rank_score",       0.0)
        # ``_expected_return`` is None for a panel-only candidate that
        # PanelScoringJob has not scored yet (orch#1082): carry the absence
        # through instead of inventing a 0.0 forecast with no horizon.
        er   = getattr(tc, "_expected_return",  0.0)
        er_h = getattr(tc, "_expected_return_horizon_days", None)
        er_txt = f"{er:+.4f}" if er is not None else "none"
        raw_txt = f"{raw:.3f}" if raw is not None else "none"
        rank_txt = f"{rank:.3f}" if rank is not None else "none"
        tc.candidate = CandidateResult(
            ticker          = tc.ticker,
            raw_score       = raw,
            rank_score      = rank,
            rs_score        = tc.rs_score,
            detail          = (f"raw={raw_txt} rank={rank_txt} "
                               f"rs={tc.rs_score:.3f} er={er_txt}"),
            expected_return = er,
            expected_return_horizon_days=er_h,
        )
        log.debug("AssembleCandidateTask [%s]: candidate assembled", tc.ticker)
