"""StrategyConfig schema tests — typo classes die at load (eng plan §III.2, S1-PR3).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.2;
prototype: scripts/engineering/config_schema_prototype.py (PR #112 batch),
proven against the real production strategy_config.json + golden.

The fixture mirrors the production top level (2026-06-12: 64 top keys,
142-ticker watchlist, regime block with 29 keys — only the dangerous
subset is typed; the rest flows through extra="allow" with telemetry).
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.config_schema import (
    ConfigSchemaError,
    StrategyConfigSchema,
    validate_strategy_config,
)


def _valid_config() -> dict:
    return {
        "model_name": "renquant-104",
        "watchlist": ["AAPL", "MU", "GE"],
        "benchmark": "SPY",
        "wash_sale_days": 30,
        "min_hold_days": 5,
        "max_hold_days": 500,
        "max_concurrent_positions": 8,
        "regime": {
            "bear_vol_threshold": 0.028,
            "bear_return_threshold": -0.018,
            "bear_vol_threshold_5d": 0.032,
            "bear_return_threshold_5d": -0.025,
            "transition_uncertainty_bars": 3,
            "bear_short_route_require_both": True,
            # untyped regime keys flow through (production has ~23 more)
            "cusum_threshold": 0.05,
            "gmm_artifact": "artifacts/gmm.json",
        },
        # untyped top-level keys flow through (production has ~56 more)
        "live": {"broker_side_stops": {"enabled": True, "pct": 0.20}},
        "regime_params": {"BULL_CALM": {"max_single_day_loss_pct": 0.06}},
    }


class TestValidConfig:

    def test_production_shape_passes(self):
        report = validate_strategy_config(_valid_config())
        assert report.ok
        assert report.errors == ()
        assert "live" in report.extra_top_keys
        assert "regime_params" in report.extra_top_keys
        assert isinstance(report.config, StrategyConfigSchema)

    def test_untyped_regime_keys_preserved(self):
        report = validate_strategy_config(_valid_config())
        assert report.config.regime.model_extra["cusum_threshold"] == 0.05


# Classic typo classes — each must be caught at load, not mid-trade.
TYPO_CASES = [
    # sign flip on a return threshold (would make BEAR unreachable)
    ("regime", "bear_return_threshold", 0.018),
    # 10× slip on a vol threshold
    ("regime", "bear_vol_threshold", 2.8),
    # impossible position cap
    (None, "max_concurrent_positions", 0),
    (None, "max_concurrent_positions", 800),
    # negative hold window
    (None, "min_hold_days", -1),
    # wash-sale beyond IRS window
    (None, "wash_sale_days", 365),
    # empty watchlist (would silently trade nothing)
    (None, "watchlist", []),
    # string-for-number that isn't a number
    ("regime", "bear_vol_threshold", "high"),
    # missing required block
    (None, "regime", None),
]


class TestTypoClasses:

    @pytest.mark.parametrize("block,key,bad", TYPO_CASES)
    def test_strict_mode_raises(self, block, key, bad):
        cfg = _valid_config()
        target = cfg[block] if block else cfg
        if bad is None:
            del target[key]
        else:
            target[key] = bad
        with pytest.raises(ConfigSchemaError, match="fail-closed"):
            validate_strategy_config(cfg, mode="strict")

    @pytest.mark.parametrize("block,key,bad", TYPO_CASES)
    def test_warn_mode_reports_never_raises(self, block, key, bad):
        cfg = _valid_config()
        target = cfg[block] if block else cfg
        if bad is None:
            del target[key]
        else:
            target[key] = bad
        report = validate_strategy_config(cfg)  # default warn
        assert not report.ok
        assert report.errors
        assert key in " ".join(report.errors)


class TestModeContract:

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="mode"):
            validate_strategy_config(_valid_config(), mode="yolo")

    def test_error_messages_carry_location_and_value(self):
        cfg = _valid_config()
        cfg["regime"]["bear_return_threshold"] = 0.5
        report = validate_strategy_config(cfg)
        assert any("regime.bear_return_threshold" in e and "0.5" in e
                   for e in report.errors)
