"""P-CONFIG-SCHEMA preflight wiring tests (eng plan §III.2, S1-PR3 rollout).

Pins the warn-first contract: schema violations surface in the preflight
slate as SOFT findings — visible, logged, never aborting — until the
deliberate strict flip after one clean telemetry week.
"""
from __future__ import annotations

from renquant_pipeline.kernel.preflight_pipeline import (
    ConfigSchemaTask,
    PreflightContext,
)


def _valid_config() -> dict:
    return {
        "model_name": "renquant-104",
        "watchlist": ["AAPL", "MU"],
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
        },
        # runtime-injected keys must pass through extra="allow"
        "_strategy_dir": "/tmp/x",
        "_run_mode": "full",
    }


def _ctx(config: dict) -> PreflightContext:
    return PreflightContext(config=config, strategy_dir=None,
                            broker=None, broker_name=None, run_mode="full")


class TestConfigSchemaTask:

    def test_valid_config_passes_with_telemetry(self):
        result = ConfigSchemaTask().check(_ctx(_valid_config()))
        assert result.ok
        assert result.severity == "soft"
        assert "_strategy_dir" in result.details["extra_top_keys"]

    def test_violation_is_soft_finding_not_abort(self):
        cfg = _valid_config()
        cfg["regime"]["bear_return_threshold"] = 0.018  # sign flip
        result = ConfigSchemaTask().check(_ctx(cfg))
        assert not result.ok
        assert result.severity == "soft", \
            "warn-first window: schema violations must not hard-fail"
        assert "bear_return_threshold" in result.message
        assert result.details["errors"]

    def test_check_name_registered_in_legacy_order(self):
        from renquant_pipeline.kernel.preflight import _LEGACY_CHECK_ORDER

        assert "P-CONFIG-SCHEMA" in _LEGACY_CHECK_ORDER

    def test_task_registered_in_pipeline(self):
        from renquant_pipeline.kernel.preflight_pipeline.pipeline import (
            build_preflight_pipeline,
        )

        names = [type(t).__name__
                 for job in build_preflight_pipeline().jobs
                 for t in job.tasks]
        assert "ConfigSchemaTask" in names


class TestStrictModePreservation:
    """run_preflight(strict=True) must NOT raise on a schema violation —
    soft findings never enter PreflightFailed."""

    def test_soft_violation_does_not_raise_in_strict(self):
        from renquant_pipeline.kernel.preflight_pipeline.base import (
            PreflightPipeline,
        )
        from renquant_pipeline.kernel.preflight_pipeline.pipeline import (
            _RiskConfigJob,
        )

        cfg = _valid_config()
        cfg["max_concurrent_positions"] = 800  # impossible cap
        ctx = _ctx(cfg)
        results = PreflightPipeline(jobs=[_RiskConfigJob()]).run(ctx, strict=True)
        schema = [r for r in results if r.name == "P-CONFIG-SCHEMA"]
        assert len(schema) == 1
        assert not schema[0].ok
        assert schema[0].severity == "soft"
