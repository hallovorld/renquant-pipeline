"""Per-candidate + per-holding data-integrity gate (WARN-first).

2026-06-23. Buy candidates and holdings can be scored on heavily *imputed*
inputs: when a name's fundamental features are missing they are
cross-sectionally median-imputed during scoring, so the model's rank for an
incomplete name rests partly on filled-in values rather than real evidence
(NFLX was missing 3/5 fundamentals the day it was ordered; the universe was
only 57/829 fundamentally complete). This task measures input quality for
exactly the names a decision touches — the ranked buy candidates and the
current book — along TWO independent dimensions and down-weights / flags the
unreliable ones:
  - COMPLETENESS: fraction of fundamental cols present (non-NaN) in the latest row;
  - FRESHNESS (as-of age): how old that latest row is. These are SEPARATE
    failure modes — a 5/5-complete row from a 91-day-stale panel is exactly the
    2026-06-23 incident and must NOT pass just because it is "complete".

WARN-first policy (operator decision 2026-06-23):
  - buy candidates below the completeness floor OR older than the staleness
    ceiling are DOWN-WEIGHTED (rank_score + alpha fields shrunk, mirroring the
    regime-momentum gate) and flagged with a per-dimension reason — they are NOT
    hard-blocked in this mode;
  - holdings are FLAGGED ONLY (never auto-acted) for operator / exit visibility.

Visibility: per-name records (ticker, completeness, age_days, reason) are
written to ``ctx._data_integrity_report`` (candidates_penalized /
holdings_degraded) for the daily bundle / ntfy / execution audit, plus the
per-candidate ``quality_penalty_reasons`` that already flows to the decision
trace, plus ``ctx.counters``.

Default OFF. Opt-in via ``ranking.data_integrity.enabled``. Escalation to a hard
fail-closed block for new buys (holdings stay flag-only) is a later config
change once a replay/ablation over historical candidate lists (current vs
warn/down-weight vs block: order diffs, skipped buys, turnover, forward returns,
drawdown) validates it. The universe-level fail-closed control is the separate
``P-DATA-FRESHNESS`` preflight; this task is the per-name companion.

Config (``ranking.data_integrity``):
  enabled: bool                   (default False)
  min_fund_completeness: float    (default 0.6 — fraction of fundamental cols
                                  that must be non-NaN to avoid a penalty)
  max_fund_age_days: int|None     (default 45 — penalize rows older than this
                                  even when complete; 0/None disables freshness)
  penalty_scale: float            (default 0.5 — shrink factor, higher-is-better)
  propagate_to_alpha_fields: bool (default True — also shrink mu/expected_return
                                  so sizing can't undo the quality penalty)
  alpha_attrs: list[str]          (default ["mu", "expected_return"])
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from renquant_pipeline.kernel.pipeline.pipeline import Task
from renquant_pipeline.kernel.pipeline.task_buy_quality_gates import (
    _penalize_higher_is_better,
)

log = logging.getLogger("kernel.pipeline.data_integrity")

_FUND_COLS = ("earnings_yield", "book_to_price", "gross_profitability",
              "roe", "asset_growth")


def fundamental_completeness(fund_panel: "pd.DataFrame | None",
                             tickers) -> dict[str, float]:
    """Map each ticker → fraction of fundamental cols present (non-NaN) in its
    latest row. A missing ticker or empty panel → 0.0 (treated as fully
    imputed)."""
    tickers = list(tickers)
    if fund_panel is None or getattr(fund_panel, "empty", True):
        return {t: 0.0 for t in tickers}
    cols = [c for c in _FUND_COLS if c in fund_panel.columns]
    if not cols or "ticker" not in fund_panel.columns:
        return {t: 0.0 for t in tickers}
    panel = fund_panel
    if "date" in panel.columns:
        panel = panel.sort_values("date")
    latest = panel.groupby("ticker").tail(1).set_index("ticker")
    out: dict[str, float] = {}
    for t in tickers:
        if t in latest.index:
            row = latest.loc[t, cols]
            out[t] = float(pd.Series(row).notna().sum()) / len(cols)
        else:
            out[t] = 0.0
    return out


def fundamental_age_days(fund_panel: "pd.DataFrame | None", tickers,
                         today) -> dict[str, "int | None"]:
    """Map each ticker → age in days of its latest fundamental row vs ``today``.

    Completeness and freshness are SEPARATE failure modes: a row can be 5/5
    complete yet 91 days stale (the 2026-06-23 incident). ``None`` when the
    ticker has no row or no usable date / today is unknown."""
    tickers = list(tickers)
    if (fund_panel is None or getattr(fund_panel, "empty", True)
            or today is None or "ticker" not in getattr(fund_panel, "columns", [])
            or "date" not in fund_panel.columns):
        return {t: None for t in tickers}
    today_ts = pd.Timestamp(today).normalize()
    latest = fund_panel.sort_values("date").groupby("ticker").tail(1).set_index("ticker")
    out: dict[str, int | None] = {}
    for t in tickers:
        if t in latest.index:
            d = pd.to_datetime(latest.loc[t, "date"], errors="coerce")
            out[t] = None if pd.isna(d) else max(0, (today_ts - d.normalize()).days)
        else:
            out[t] = None
    return out


def _load_fund_panel(ctx: Any):
    """Read sec_fundamentals_daily decoupled from the scoring hot-path. Returns
    None if unavailable. Factored out as a seam so the task is unit-testable."""
    from renquant_pipeline.kernel.panel_pipeline._data_root import (  # noqa: PLC0415
        data_root as _data_root,
    )
    from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (  # noqa: PLC0415
        _cached_parquet,
    )
    fp = _data_root() / "data" / "sec_fundamentals_daily.parquet"
    if not fp.exists():
        return None
    return _cached_parquet(ctx, ("sec_fundamentals_daily", str(fp)), fp)


class DataIntegrityTask(Task):
    """Down-weight low-completeness buy candidates and flag degraded holdings."""
    name = "DataIntegrityTask"

    def run(self, ctx) -> bool | None:
        cfg = (ctx.config or {}).get("ranking", {}).get("data_integrity", {})
        if not cfg.get("enabled", False):
            return
        floor = float(cfg.get("min_fund_completeness", 0.6))
        # 0 / None disables the staleness dimension; default 45d mirrors the
        # P-DATA-FRESHNESS critical threshold (a 5/5-complete-but-91d-stale row
        # is the exact failure that motivated this gate).
        max_age = cfg.get("max_fund_age_days", 45)
        max_age = float(max_age) if max_age else None
        penalty = float(cfg.get("penalty_scale", 0.5))
        propagate = bool(cfg.get("propagate_to_alpha_fields", True))
        alpha_attrs = list(cfg.get("alpha_attrs", ["mu", "expected_return"]) or [])

        candidates = list(getattr(ctx, "candidates", []) or [])
        holdings = getattr(ctx, "holdings", {}) or {}
        held = list(holdings.keys()) if hasattr(holdings, "keys") else list(holdings)
        names = {c.ticker for c in candidates} | set(held)
        if not names:
            return

        panel = _load_fund_panel(ctx)
        completeness = fundamental_completeness(panel, names)
        age = fundamental_age_days(panel, names, getattr(ctx, "today", None))

        def _degraded(t):
            """(is_degraded, reason) for ticker t on EITHER dimension."""
            if completeness.get(t, 0.0) < floor:
                return True, "data_integrity_low_completeness"
            a = age.get(t)
            if max_age is not None and a is not None and a > max_age:
                return True, "data_integrity_stale_fundamentals"
            return False, None

        attrs = ["rank_score"] + (alpha_attrs if propagate else [])
        penalized: list[tuple[str, float, "int | None", str]] = []
        for cand in candidates:
            bad, reason = _degraded(cand.ticker)
            if not bad:
                continue
            changed = False
            for a in attrs:
                old = getattr(cand, a, None)
                if old is None:
                    continue
                try:
                    old_f = float(old)
                except (TypeError, ValueError):
                    continue
                if old_f != old_f:  # NaN
                    continue
                setattr(cand, a, _penalize_higher_is_better(old_f, penalty))
                changed = True
            if not changed:
                continue
            prior = getattr(cand, "quality_multiplier", 1.0)
            try:
                prior_f = float(prior)
            except (TypeError, ValueError):
                prior_f = 1.0
            cand.quality_multiplier = prior_f * penalty
            reasons = list(getattr(cand, "quality_penalty_reasons", []) or [])
            reasons.append(reason)
            cand.quality_penalty_reasons = reasons
            penalized.append((cand.ticker, completeness.get(cand.ticker, 0.0),
                              age.get(cand.ticker), reason))

        degraded = [{"ticker": t, "completeness": completeness.get(t, 0.0),
                     "age_days": age.get(t), "reason": _degraded(t)[1]}
                    for t in held if _degraded(t)[0]]
        # surfaced for the daily bundle / ntfy / execution audit (operator
        # visibility): structured per-name records, not just a counter.
        ctx._data_integrity_degraded_holdings = [d["ticker"] for d in degraded]
        ctx._data_integrity_report = {
            "candidates_penalized": [
                {"ticker": t, "completeness": c, "age_days": ag, "reason": r}
                for (t, c, ag, r) in penalized],
            "holdings_degraded": degraded,
        }

        if penalized or degraded:
            log.info(
                "DataIntegrity: %d/%d candidate(s) down-weighted ×%.2f "
                "(completeness<%.0f%% or fundamentals>%sd stale), %d holding(s) flagged",
                len(penalized), len(candidates), penalty, floor * 100,
                int(max_age) if max_age else "∞", len(degraded),
            )
            for t, c, ag, r in penalized[:8]:
                log.info("  candidate %s completeness=%.0f%% age=%sd reason=%s",
                         t, c * 100, ag if ag is not None else "?", r)
            if degraded:
                log.info("  degraded holdings (flag only): %s",
                         ", ".join(f"{d['ticker']}({d['reason'].split('_')[-1]})"
                                   for d in degraded[:12]))
        ctx.counters = getattr(ctx, "counters", None) or {}
        ctx.counters["data_integrity_candidates_penalized"] = len(penalized)
        ctx.counters["data_integrity_holdings_flagged"] = len(degraded)
