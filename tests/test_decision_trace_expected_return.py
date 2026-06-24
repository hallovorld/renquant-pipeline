"""The LIVE decision-trace builder — `renquant_pipeline.decision_trace`, the one
`inference.py` uses to build the run bundle's trace — must record
`expected_return` (mu), the quantity `ConvictionGateTask` floors. Without it the
accumulating decision-ledger cannot validate a gate change on real admitted sets.
"""
from __future__ import annotations

from types import SimpleNamespace

from renquant_pipeline.decision_trace import build_ticker_daily_state_rows


def _ctx(candidates):
    return SimpleNamespace(
        candidates=candidates, scores={}, panel_scores={}, rank_scores={},
        account_snapshot={}, blocked_by={},
    )


def test_live_trace_row_carries_expected_return_mu():
    ctx = _ctx([
        SimpleNamespace(ticker="AAPL", expected_return=0.042),
        SimpleNamespace(ticker="MSFT", expected_return=-0.011),
    ])
    cfg = {"watchlist": ["AAPL", "MSFT"], "sector_map": {}}
    rows = build_ticker_daily_state_rows(config=cfg, ctx=ctx)
    by = {r["ticker"]: r for r in rows}
    assert by["AAPL"]["expected_return"] == 0.042
    assert by["MSFT"]["expected_return"] == -0.011


def test_live_trace_expected_return_from_explicit_mapping():
    # a pre-built ctx.expected_returns mapping is honored over candidates
    ctx = _ctx([])
    ctx.expected_returns = {"AAPL": 0.03}
    cfg = {"watchlist": ["AAPL"], "sector_map": {}}
    rows = build_ticker_daily_state_rows(config=cfg, ctx=ctx)
    assert {r["ticker"]: r["expected_return"] for r in rows}["AAPL"] == 0.03


def test_live_trace_expected_return_none_when_absent():
    ctx = _ctx([])
    cfg = {"watchlist": ["AAPL"], "sector_map": {}}
    rows = build_ticker_daily_state_rows(config=cfg, ctx=ctx)
    assert all("expected_return" in r and r["expected_return"] is None for r in rows)
