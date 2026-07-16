"""Regression coverage for opt-in panel-only candidate admission."""
from __future__ import annotations

import datetime
from types import SimpleNamespace

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.pp_inference import _buy_universe
from renquant_pipeline.kernel.pipeline.task_candidates import (
    BuildFeaturesTask,
    ScoreBuyTask,
)


def _ctx(*, panel_mode: bool) -> InferenceContext:
    config = {
        "watchlist": ["AAA", "BBB", "SPY"],
        "benchmark": "SPY",
        "ranking": {
            "panel_scoring": {
                "enabled": panel_mode,
                "bypass_ticker_gate": panel_mode,
                **({"candidate_universe": "watchlist"} if panel_mode else {}),
            },
        },
    }
    ctx = InferenceContext(config=config, today=datetime.date(2026, 7, 15))
    ctx.models = {"AAA": {"_metadata": {}}}
    ctx.ohlcv = {"AAA": object(), "BBB": object(), "SPY": object()}
    ctx.holdings = {}
    ctx.pending_broker_tickers = set()
    return ctx


def test_panel_watchlist_mode_expands_candidate_universe_without_benchmark():
    assert _buy_universe(_ctx(panel_mode=True)) == ["AAA", "BBB"]


def test_legacy_candidate_universe_remains_model_backed():
    assert _buy_universe(_ctx(panel_mode=False)) == ["AAA"]


def test_panel_only_candidate_skips_tournament_feature_and_score_requirements():
    tc = SimpleNamespace(
        ticker="BBB",
        model=None,
        ohlcv={"BBB": object(), "SPY": object()},
        config=_ctx(panel_mode=True).config,
        features=None,
    )

    assert BuildFeaturesTask().run(tc) is None
    assert ScoreBuyTask().run(tc) is None
    assert tc.model_action == "panel_pending"
    assert tc._raw_score == 0.0
    assert tc._rank_score == 0.0
    assert tc._expected_return == 0.0
