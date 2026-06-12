"""P-STATE-FILE × LiveStateV2 wiring tests (eng plan §III.4, S1-PR1 rollout).

Pins the additive contract: the legacy hard gate (raw-JSON parseability)
is unchanged; typed-schema findings are SOFT during the warn window —
quarantined unknown keys and shape violations surface without aborting.
"""
from __future__ import annotations

import json

from renquant_pipeline.kernel.preflight_pipeline import (
    PreflightContext,
    StateFileTask,
)

VALID_STATE = {
    "regime": "BULL_CALM",
    "regime_confidence": 0.59,
    "high_water_mark": 11079.22,
    "entry_dates": {"MU": "2026-04-27", "GE": "2026-05-14"},
    "sell_streaks": {"MU": 0, "GE": 0},
    "last_sell_dates": {"BA": "2026-05-15"},
    "position_hwm": {"MU": 976.025},
    "entry_signals": {"MU": {"rank_score": 0.325}},
    "last_stop_exit_dates": {},
    "skip_buys": False,
    "stop_orders": {},
}


def _ctx(strategy_dir) -> PreflightContext:
    return PreflightContext(config={}, strategy_dir=strategy_dir,
                            broker=None, broker_name="alpaca", run_mode="full")


def _write_state(tmp_path, payload) -> None:
    (tmp_path / "live_state.alpaca.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload))


class TestLiveStateV2Wiring:

    def test_valid_state_hard_pass_with_holdings(self, tmp_path):
        _write_state(tmp_path, VALID_STATE)
        r = StateFileTask().check(_ctx(tmp_path))
        assert r.ok and r.severity == "hard"
        assert "2 holdings" in r.message

    def test_unknown_key_soft_pass_with_quarantine_telemetry(self, tmp_path):
        state = dict(VALID_STATE)
        state["future_field"] = {"x": 1}
        _write_state(tmp_path, state)
        r = StateFileTask().check(_ctx(tmp_path))
        assert r.ok
        assert r.severity == "soft"
        assert r.details["quarantined_keys"] == ["future_field"]

    def test_schema_violation_is_soft_not_hard(self, tmp_path):
        state = dict(VALID_STATE)
        state["entry_dates"] = "2026-04-27"  # shape corruption: str not dict
        _write_state(tmp_path, state)
        r = StateFileTask().check(_ctx(tmp_path))
        assert not r.ok
        assert r.severity == "soft", \
            "warn window: schema violations must not abort runs"
        assert "LiveStateV2" in r.message

    def test_corrupt_json_still_hard_fails(self, tmp_path):
        _write_state(tmp_path, '{"regime": "BULL')
        r = StateFileTask().check(_ctx(tmp_path))
        assert not r.ok
        assert r.severity == "hard"

    def test_absent_file_still_soft_passes(self, tmp_path):
        r = StateFileTask().check(_ctx(tmp_path))
        assert r.ok and r.severity == "soft"
        assert "first run" in r.message
