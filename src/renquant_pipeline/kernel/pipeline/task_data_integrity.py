"""Per-candidate + per-holding data-integrity gate (WARN-first).

2026-06-23. Buy candidates and holdings can be scored on heavily *imputed*
inputs: when a name's fundamental features are missing they are
cross-sectionally median-imputed during scoring, so the model's rank for an
incomplete name rests partly on filled-in values rather than real evidence
(NFLX was missing 3/5 fundamentals the day it was ordered; the universe was
only 57/829 fundamentally complete). This task measures input completeness for
exactly the names a decision touches — the ranked buy candidates and the
current book — and surfaces / down-weights the unreliable ones.

WARN-first policy (operator decision 2026-06-23):
  - buy candidates below the completeness floor are DOWN-WEIGHTED (rank_score +
    alpha fields shrunk, mirroring the regime-momentum gate) and flagged — they
    are NOT hard-blocked in this mode;
  - holdings are FLAGGED ONLY (never auto-acted) for operator / exit visibility.

Default OFF. Opt-in via ``ranking.data_integrity.enabled``. Escalating to a hard
block is a later config change once the warn signal has been observed live.

Config (``ranking.data_integrity``):
  enabled: bool                   (default False)
  min_fund_completeness: float    (default 0.6 — fraction of fundamental cols
                                  that must be non-NaN to avoid a penalty)
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
        penalty = float(cfg.get("penalty_scale", 0.5))
        propagate = bool(cfg.get("propagate_to_alpha_fields", True))
        alpha_attrs = list(cfg.get("alpha_attrs", ["mu", "expected_return"]) or [])

        candidates = list(getattr(ctx, "candidates", []) or [])
        holdings = getattr(ctx, "holdings", {}) or {}
        held = list(holdings.keys()) if hasattr(holdings, "keys") else list(holdings)
        names = {c.ticker for c in candidates} | set(held)
        if not names:
            return

        completeness = fundamental_completeness(_load_fund_panel(ctx), names)

        attrs = ["rank_score"] + (alpha_attrs if propagate else [])
        penalized: list[tuple[str, float]] = []
        for cand in candidates:
            comp = completeness.get(cand.ticker, 0.0)
            if comp >= floor:
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
            reasons.append("data_integrity_low_completeness")
            cand.quality_penalty_reasons = reasons
            penalized.append((cand.ticker, comp))

        degraded = [t for t in held if completeness.get(t, 0.0) < floor]
        ctx._data_integrity_degraded_holdings = degraded

        if penalized or degraded:
            log.info(
                "DataIntegrity: %d/%d candidate(s) down-weighted (fund "
                "completeness < %.0f%% → ×%.2f), %d holding(s) flagged",
                len(penalized), len(candidates), floor * 100, penalty, len(degraded),
            )
            for t, comp in penalized[:8]:
                log.info("  candidate %s fund_completeness=%.0f%%", t, comp * 100)
            if degraded:
                log.info("  degraded holdings (flag only): %s", ", ".join(degraded[:12]))
        ctx.counters = getattr(ctx, "counters", None) or {}
        ctx.counters["data_integrity_candidates_penalized"] = len(penalized)
        ctx.counters["data_integrity_holdings_flagged"] = len(degraded)
