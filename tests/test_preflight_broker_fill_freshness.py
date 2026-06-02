"""Regression tests for P-BROKER-FILL-FRESHNESS in renquant-pipeline.

The check must use runner-emission state, not any-source broker fills. A fresh
manual fill or broker-side stop must not reset a stale runner-driven streak.
"""
from __future__ import annotations

import datetime as dt
import json

from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.broker_fill_freshness import (
    BrokerFillFreshnessTask,
)


def _write_state(tmp_path, broker_name, monitor_state):
    state_path = tmp_path / f"live_state.{broker_name}.json"
    state_path.write_text(json.dumps({"monitor_state": monitor_state}))
    return state_path


def _ctx(tmp_path, broker_name="alpaca", cfg=None):
    return PreflightContext(
        config=cfg or {},
        strategy_dir=tmp_path,
        broker=object(),
        broker_name=broker_name,
    )


def test_no_broker_name_dry_run_soft_pass(tmp_path):
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, broker_name=None)
    result = BrokerFillFreshnessTask().check(ctx)
    assert result.ok
    assert result.severity == "soft"
    assert "dry-run" in result.message.lower()


def test_state_file_absent_soft_pass(tmp_path):
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert result.ok
    assert result.severity == "soft"
    assert "absent" in result.message.lower()


def test_recent_runner_activity_hard_pass(tmp_path):
    today = dt.date.today()
    _write_state(tmp_path, "alpaca", {
        "last_activity_date": today.isoformat(),
        "first_trade_date": "2026-04-01",
    })
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert result.ok
    assert result.severity == "hard"
    assert "last runner-driven activity" in result.message


def test_streak_between_warn_and_hard_soft_warn(tmp_path):
    old = dt.date.today() - dt.timedelta(days=14)
    _write_state(tmp_path, "alpaca", {
        "last_activity_date": old.isoformat(),
        "first_trade_date": "2026-04-01",
    })
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert result.ok
    assert result.severity == "soft"
    assert "warn cap" in result.message


def test_no_runner_activity_recorded_hard_fail(tmp_path):
    _write_state(tmp_path, "alpaca", {})
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert not result.ok
    assert result.severity == "hard"
    assert "never emitted" in result.message.lower()


def test_streak_above_hard_fails(tmp_path):
    old = dt.date.today() - dt.timedelta(days=40)
    _write_state(tmp_path, "alpaca", {
        "last_activity_date": old.isoformat(),
        "first_trade_date": "2026-04-01",
    })
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert not result.ok
    assert result.severity == "hard"
    assert "strategy is dormant" in result.message.lower()


def test_thresholds_configurable_via_cfg(tmp_path):
    old = dt.date.today() - dt.timedelta(days=14)
    _write_state(tmp_path, "alpaca", {
        "last_activity_date": old.isoformat(),
        "first_trade_date": "2026-04-01",
    })
    cfg = {
        "monitoring": {
            "fill_freshness_warn_after_trading_days": 1,
            "fill_freshness_hard_after_trading_days": 3,
        },
    }
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path, cfg=cfg))
    assert not result.ok
    assert result.severity == "hard"


def test_external_fill_yesterday_runner_stale_still_fails(tmp_path):
    runner_old = dt.date.today() - dt.timedelta(days=40)
    _write_state(tmp_path, "alpaca", {
        "last_fill_date": dt.date.today().isoformat(),
        "last_activity_date": runner_old.isoformat(),
        "first_trade_date": "2026-04-01",
        "no_trade_streak": 0,
        "no_trade_streak_source": "broker_filled_orders",
    })
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert not result.ok
    assert result.severity == "hard"
    assert "dormant" in result.message.lower()


def test_falls_back_to_first_trade_date_when_last_activity_missing(tmp_path):
    first = dt.date.today() - dt.timedelta(days=2)
    _write_state(tmp_path, "alpaca", {"first_trade_date": first.isoformat()})
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert result.ok
    assert result.severity == "hard"


def test_unparseable_activity_date_soft_pass(tmp_path):
    _write_state(tmp_path, "alpaca", {"last_activity_date": "not-a-real-date"})
    result = BrokerFillFreshnessTask().check(_ctx(tmp_path))
    assert result.ok
    assert result.severity == "soft"
    assert "unparseable" in result.message.lower()
