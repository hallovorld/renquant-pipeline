from __future__ import annotations

import datetime as dt

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.job_selection import SelectionJob
from renquant_pipeline.kernel.pipeline.task_selection import ApplyBearDefensiveSleeveTask


def _ctx(config: dict | None = None, **overrides) -> InferenceContext:
    base_config = {
        "defensive_tickers": ["GLD", "TLT", "SHY"],
        "bear_defensive_pct": 0.20,
        "bear_defensive_slots": 2,
        "bear_defensive_sleeve": {"enabled": True},
        "regime_params": {
            "BEAR": {
                "cash_reserve_pct": 0.0,
                "max_concurrent_positions": 8,
            },
        },
    }
    if config:
        base_config.update(config)
    values = {
        "config": base_config,
        "today": dt.date(2026, 6, 6),
        "regime": "BEAR",
        "bear_only": True,
        "portfolio_value": 10_000.0,
        "cash": 10_000.0,
        "prices": {"GLD": 50.0, "TLT": 100.0, "SHY": 25.0},
        "ranked": [],
        "models": {},
    }
    values.update(overrides)
    return InferenceContext(**values)


def test_bear_defensive_sleeve_runs_without_ranked_models() -> None:
    ctx = _ctx()

    SelectionJob().run(ctx)

    assert [order["ticker"] for order in ctx.orders] == ["GLD", "TLT"]
    assert [order["shares"] for order in ctx.orders] == [20, 10]
    assert ctx.counters["bear_defensive_sleeve_orders"] == 2


def test_bear_defensive_sleeve_is_default_off() -> None:
    ctx = _ctx(config={"bear_defensive_sleeve": {"enabled": False}})

    SelectionJob().run(ctx)

    assert ctx.orders == []


def test_bear_defensive_sleeve_skips_non_bear_only_context() -> None:
    ctx = _ctx(bear_only=False)

    SelectionJob().run(ctx)

    assert ctx.orders == []


def test_bear_defensive_sleeve_uses_fixed_slot_cap_and_excludes_held() -> None:
    ctx = _ctx(holdings={"GLD": object()})

    SelectionJob().run(ctx)

    assert [order["ticker"] for order in ctx.orders] == ["TLT"]
    assert ctx.orders[0]["shares"] == 10
    assert ctx.orders[0]["target_pct"] == 0.10


def test_bear_defensive_sleeve_respects_remaining_cash_and_reserve() -> None:
    ctx = _ctx(
        cash=1_500.0,
        orders=[{"ticker": "AAPL", "invest": 200.0}],
        config={
            "regime_params": {
                "BEAR": {
                    "cash_reserve_pct": 0.10,
                    "max_concurrent_positions": 8,
                },
            },
        },
    )

    ApplyBearDefensiveSleeveTask().run(ctx)

    assert [order["ticker"] for order in ctx.orders] == ["AAPL", "GLD"]
    assert ctx.orders[-1]["shares"] == 6
    assert ctx.orders[-1]["invest"] == 300.0


def test_bear_defensive_sleeve_does_not_repeat_existing_ordered_ticker() -> None:
    ctx = _ctx(orders=[{"ticker": "GLD", "invest": 500.0}])

    ApplyBearDefensiveSleeveTask().run(ctx)

    assert [order["ticker"] for order in ctx.orders] == ["GLD", "TLT"]


def test_bear_defensive_sleeve_respects_total_position_slots() -> None:
    ctx = _ctx(
        holdings={"AAPL": object(), "MSFT": object()},
        config={
            "regime_params": {
                "BEAR": {
                    "cash_reserve_pct": 0.0,
                    "max_concurrent_positions": 2,
                },
            },
        },
    )

    SelectionJob().run(ctx)

    assert ctx.orders == []


def test_bear_defensive_sleeve_stamps_auditable_order_attribution() -> None:
    ctx = _ctx(config={"bear_defensive_slots": 1})

    SelectionJob().run(ctx)

    order = ctx.orders[0]
    assert order["order_type"] == "BEAR_DEFENSIVE_SLEEVE"
    assert order["order_source"] == "BEAR_DEFENSIVE_SLEEVE"
    assert order["source_job"] == "SelectionJob"
    assert order["source_task"] == "ApplyBearDefensiveSleeveTask"
    assert order["decision_inputs"]["acceptance_reason"] == "bear_defensive_sleeve_enabled"
    assert order["decision_inputs"]["slot_pct"] == 0.20
