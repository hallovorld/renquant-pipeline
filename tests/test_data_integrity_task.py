"""Tests for the per-candidate + per-holding data-integrity gate."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.pipeline import task_data_integrity as di
from renquant_pipeline.kernel.pipeline.task_data_integrity import (
    DataIntegrityTask,
    fundamental_completeness,
)


def _panel():
    # AAPL complete (5/5), NFLX 2/5, ZM 1/5
    rows = [
        {"ticker": "AAPL", "date": pd.Timestamp("2026-06-23"), "earnings_yield": 0.05,
         "book_to_price": 0.1, "gross_profitability": 0.3, "roe": 0.2, "asset_growth": 0.1},
        {"ticker": "NFLX", "date": pd.Timestamp("2026-06-23"), "earnings_yield": None,
         "book_to_price": 0.07, "gross_profitability": None, "roe": None, "asset_growth": 0.04},
        {"ticker": "ZM", "date": pd.Timestamp("2026-06-23"), "earnings_yield": None,
         "book_to_price": None, "gross_profitability": None, "roe": None, "asset_growth": 0.09},
    ]
    return pd.DataFrame(rows)


# ── fundamental_completeness() ────────────────────────────────────────────
def test_completeness_counts_non_nan():
    comp = fundamental_completeness(_panel(), ["AAPL", "NFLX", "ZM", "MISSING"])
    assert comp["AAPL"] == 1.0
    assert comp["NFLX"] == pytest.approx(0.4)   # 2/5
    assert comp["ZM"] == pytest.approx(0.2)     # 1/5
    assert comp["MISSING"] == 0.0               # not in panel → fully imputed


def test_completeness_empty_panel_all_zero():
    assert fundamental_completeness(None, ["AAPL"]) == {"AAPL": 0.0}
    assert fundamental_completeness(pd.DataFrame(), ["AAPL"]) == {"AAPL": 0.0}


# ── DataIntegrityTask ─────────────────────────────────────────────────────
def _cand(ticker, rank_score=0.6, mu=0.04):
    return SimpleNamespace(ticker=ticker, rank_score=rank_score, mu=mu,
                           expected_return=mu)


def _ctx(candidates, holdings, enabled=True, today=None, **cfg):
    return SimpleNamespace(
        config={"ranking": {"data_integrity": {"enabled": enabled,
                                               "min_fund_completeness": 0.6,
                                               "penalty_scale": 0.5, **cfg}}},
        candidates=candidates, holdings=holdings, today=today)


def test_low_completeness_candidate_is_downweighted(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    aapl, nflx = _cand("AAPL"), _cand("NFLX")
    ctx = _ctx([aapl, nflx], {})
    DataIntegrityTask().run(ctx)
    # AAPL complete → untouched
    assert aapl.rank_score == 0.6 and aapl.mu == 0.04
    assert not getattr(aapl, "quality_penalty_reasons", [])
    # NFLX 2/5 < 0.6 floor → rank_score + mu shrunk ×0.5, flagged
    assert nflx.rank_score == pytest.approx(0.3)
    assert nflx.mu == pytest.approx(0.02)
    assert "data_integrity_low_completeness" in nflx.quality_penalty_reasons
    assert ctx.counters["data_integrity_candidates_penalized"] == 1


def test_stale_but_complete_candidate_is_downweighted(monkeypatch):
    # AAPL is 5/5 complete but the panel is 92 days old → must still be penalized
    # (the 2026-06-23 incident: a complete-but-stale row slipping through).
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())  # dated 2026-06-23
    aapl = _cand("AAPL")
    ctx = _ctx([aapl], {}, today=date(2026, 9, 23), max_fund_age_days=45)
    DataIntegrityTask().run(ctx)
    assert aapl.rank_score == pytest.approx(0.3)  # down-weighted despite 5/5
    assert "data_integrity_stale_fundamentals" in aapl.quality_penalty_reasons


def test_fresh_complete_candidate_untouched(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    aapl = _cand("AAPL")
    ctx = _ctx([aapl], {}, today=date(2026, 6, 24), max_fund_age_days=45)  # 1d old
    DataIntegrityTask().run(ctx)
    assert aapl.rank_score == 0.6  # complete + fresh → untouched


def test_fundamental_age_days(monkeypatch):
    age = di.fundamental_age_days(_panel(), ["AAPL", "MISSING"], date(2026, 6, 30))
    assert age["AAPL"] == 7
    assert age["MISSING"] is None
    # no today / no panel → None
    assert di.fundamental_age_days(_panel(), ["AAPL"], None)["AAPL"] is None


def test_report_surfaced_for_bundle(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    ctx = _ctx([_cand("NFLX")], {"ZM": object()})
    DataIntegrityTask().run(ctx)
    rep = ctx._data_integrity_report
    assert rep["candidates_penalized"][0]["ticker"] == "NFLX"
    assert rep["candidates_penalized"][0]["reason"] == "data_integrity_low_completeness"
    assert rep["holdings_degraded"][0]["ticker"] == "ZM"


def test_holdings_are_flagged_only_not_acted(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    ctx = _ctx([], {"ZM": object(), "AAPL": object()})
    DataIntegrityTask().run(ctx)
    # ZM 1/5 < floor → flagged; AAPL complete → not flagged
    assert ctx._data_integrity_degraded_holdings == ["ZM"]
    assert ctx.counters["data_integrity_holdings_flagged"] == 1


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    nflx = _cand("NFLX")
    ctx = _ctx([nflx], {}, enabled=False)
    DataIntegrityTask().run(ctx)
    assert nflx.rank_score == 0.6  # untouched
    assert not hasattr(ctx, "counters")


def test_no_alpha_propagation_when_disabled(monkeypatch):
    monkeypatch.setattr(di, "_load_fund_panel", lambda ctx: _panel())
    nflx = _cand("NFLX")
    ctx = _ctx([nflx], {}, propagate_to_alpha_fields=False)
    DataIntegrityTask().run(ctx)
    assert nflx.rank_score == pytest.approx(0.3)  # rank shrunk
    assert nflx.mu == 0.04  # alpha untouched
