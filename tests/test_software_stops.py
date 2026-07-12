"""S-FRAC stage 3 (core, sprint D2) — software-stop layer tests, pipeline side.

Companion to renquant-orchestrator's ``backtesting/renquant_104/tests/test_software_stops.py``
(the Phase-1 byte-equivalent-mirror pair for
``kernel/pipeline/task_software_stops.py``).

Relocation (2026-07-04): the registry itself
(``renquant_pipeline.software_stops.SoftwareStopRegistry``) now lives in
THIS repo (moved from the umbrella's ``adapters/software_stops.py`` per the
architectural-ownership finding on RenQuant#440 — new capability logic
belongs in an owning repo, not the umbrella). This file therefore carries
two kinds of coverage:

* ``TestTaxonomyMembership``/``TestSellOnlyLoopWiring``/``TestFlagOffInert``
  (original, kept as-is): ``SoftwareStopExitTask``'s own contract, tested
  against a FAKE duck-typed registry (``is_armed()`` + ``evaluate(prices)``)
  — the task never imports the registry class directly, only reads
  ``ctx.software_stops`` via ``getattr``, so this remains a faithful,
  registry-implementation-agnostic test of the task's behavior.
* ``TestRegistryRoundTrip``/``TestRatchetOnlyInvariant``/
  ``TestTriggerCorrectness``/``TestGapThroughPricing``/
  ``TestCorruptRegistryFailClosed``/``TestStalenessWatchdog`` (new): direct
  unit tests of the now-relocated ``SoftwareStopRegistry`` class itself.

The umbrella's own ``tests/test_software_stops.py`` retains the
orchestration-wiring coverage neither of the above carries: the real
``RunnerAdapter.commit`` E2E path, the stage-0 capability-gate integration
test, and the ops watchdog CLI script's own exit codes.

Design: renquant-orchestrator doc/design/2026-07-02-s-frac-fractional-v2.md
§3.2 (registry + sell-only-loop delta) / §3.3 (failure modes) / §3.4
(staleness watchdog).
"""
from __future__ import annotations

import datetime
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from renquant_pipeline.kernel.exit_types import (
    META_LABEL_VETO_ELIGIBLE,
    PANEL_VETO_BYPASS,
    PER_BAR_CAP_EXEMPT,
    POST_STOP_COOLDOWN_TRIGGERS,
)
from renquant_pipeline.kernel.pipeline.pp_inference import SellOnlyPipeline
from renquant_pipeline.kernel.pipeline.task_software_stops import SoftwareStopExitTask
from renquant_pipeline.software_stops import (
    DEFAULT_MAX_STALENESS_MINUTES,
    SoftwareStopRegistry,
    SoftwareStopRegistryCorrupt,
    _validate_snapshot,
    compute_staleness,
    registry_path_for,
    validate_software_stop_snapshot,
)

FRACTIONAL_QTY = 0.435578

NOW = datetime.datetime(2026, 7, 3, 11, 0, tzinfo=datetime.timezone.utc)


def _registry(tmp_path, **kwargs) -> SoftwareStopRegistry:
    return SoftwareStopRegistry(
        tmp_path / "data" / "rq105" / "software_stops.json", **kwargs
    )


def _armed_with(tmp_path, symbol="BLK", qty=FRACTIONAL_QTY, stop=760.0,
                source="z9") -> SoftwareStopRegistry:
    reg = _registry(tmp_path)
    reg.register(symbol, qty, stop, source=source, today_str="2026-07-03")
    return reg


class _FakeRegistry:
    """Minimal duck-typed stand-in for adapters.software_stops.SoftwareStopRegistry."""

    def __init__(self, *, armed, breaches=None):
        self._armed = armed
        self._breaches = breaches or []

    def is_armed(self):
        return self._armed

    def evaluate(self, prices):  # noqa: ARG002 — fixed breach list per test
        return self._breaches


def _ctx(registry, prices):
    return SimpleNamespace(software_stops=registry, prices=dict(prices), exits=[])


class TestTaxonomyMembership:
    def test_software_stop_bypasses_veto_and_cap_but_not_meta_label_eligible(self):
        """A software stop is a stop: bypasses panel veto + per-bar cap,
        triggers the post-stop re-entry blackout, and is NOT meta-label
        vetoable (only canonical core types are)."""
        assert "software_stop" in PANEL_VETO_BYPASS
        assert "software_stop" in PER_BAR_CAP_EXEMPT
        assert "software_stop" in POST_STOP_COOLDOWN_TRIGGERS
        assert "software_stop" not in META_LABEL_VETO_ELIGIBLE


class TestSellOnlyLoopWiring:
    def test_breach_appends_software_stop_exit(self):
        reg = _FakeRegistry(
            armed=True,
            breaches=[{"symbol": "BLK", "qty": 0.35, "reason": "software_stop breach: price 700.0 <= stop 760.0"}],
        )
        ctx = _ctx(reg, {"BLK": 700.0})
        SoftwareStopExitTask().run(ctx)
        assert len(ctx.exits) == 1
        ticker, sig = ctx.exits[0]
        assert ticker == "BLK"
        assert sig.should_exit is True
        assert sig.exit_type == "software_stop"
        assert sig.quantity == 0.35
        assert "software_stop breach" in sig.reason

    def test_no_breach_no_exit(self):
        reg = _FakeRegistry(armed=True, breaches=[])
        ctx = _ctx(reg, {"BLK": 800.0})
        SoftwareStopExitTask().run(ctx)
        assert ctx.exits == []

    def test_unarmed_registry_is_loud_noop(self, caplog):
        reg = _FakeRegistry(armed=False)
        ctx = _ctx(reg, {"BLK": 1.0})
        with caplog.at_level("ERROR", logger="kernel.pipeline"):
            SoftwareStopExitTask().run(ctx)
        assert ctx.exits == []
        assert "NOT armed" in caplog.text

    def test_sell_only_pipeline_runs_task_after_veto_and_cap(self):
        """Source-order pin: the software-stop pass runs AFTER the
        meta-label veto and the per-bar sell cap (a broker-resident stop
        can't be vetoed or capped; nor can its software mirror). Mirrors
        the umbrella's identical assertion on its own SellOnlyPipeline."""
        src = inspect.getsource(SellOnlyPipeline.run)
        i_veto = src.index("MetaLabelVetoTask().run")
        i_cap = src.index("LimitSellsPerBarTask().run")
        i_sw = src.index("SoftwareStopExitTask().run")
        assert i_veto < i_sw
        assert i_cap < i_sw


class TestFlagOffInert:
    def test_task_noop_without_registry(self, tmp_path):
        """Flag-off byte-inertness on the sell-only loop: no registry on
        ctx ⇒ no exits appended, nothing written anywhere."""
        for ctx in (
            SimpleNamespace(prices={"BLK": 1.0}, exits=[]),           # attr absent
            SimpleNamespace(software_stops=None,
                            prices={"BLK": 1.0}, exits=[]),           # attr None
        ):
            task = SoftwareStopExitTask()
            assert task.should_skip(ctx) is True
            task.run(ctx)
            assert ctx.exits == []
        assert list(tmp_path.iterdir()) == []          # no state file created


# ═════════════════════════════════════════════════════════════════════════════
# Registry schema + round-trip
# ═════════════════════════════════════════════════════════════════════════════

class TestRegistryRoundTrip:
    def test_register_persists_schema_and_reloads(self, tmp_path):
        reg = _armed_with(tmp_path)
        assert reg.is_armed() is True

        raw = json.loads(reg.path.read_text())
        assert raw["version"] == 1
        assert raw["contract"] == "software-stops-v1"
        assert "max_staleness_minutes" in raw
        entry = raw["stops"]["BLK"]
        assert entry["symbol"] == "BLK"
        assert entry["qty"] == FRACTIONAL_QTY          # float verbatim
        assert entry["stop_price"] == 760.0
        assert entry["armed_at"] == "2026-07-03"
        assert entry["source"] == "z9"
        assert entry["history"][0]["action"] == "register"

        # Fresh instance loads the identical protection surface.
        reloaded = SoftwareStopRegistry(reg.path)
        assert reloaded.is_armed() is True
        assert reloaded.get("BLK")["stop_price"] == 760.0
        assert reloaded.get("BLK")["qty"] == FRACTIONAL_QTY

    def test_no_file_until_first_write(self, tmp_path):
        reg = _registry(tmp_path)
        assert reg.is_armed() is True   # empty registry is armed
        assert not reg.path.exists()    # …but writes nothing until used

    def test_invalid_source_rejected(self, tmp_path):
        reg = _registry(tmp_path)
        with pytest.raises(ValueError, match="source"):
            reg.register("BLK", 1.0, 80.0, source="cosmic-ray")

    def test_from_config_flag_off_returns_none(self):
        assert SoftwareStopRegistry.from_config(None) is None
        assert SoftwareStopRegistry.from_config({}) is None
        cfg = {"execution": {"software_stops": {"enabled": False}}}
        assert SoftwareStopRegistry.from_config(cfg) is None

    def test_from_config_broker_tagged_path(self, tmp_path):
        cfg = {"execution": {"software_stops": {
            "enabled": True,
            "registry_path": "data/rq105/software_stops.json",
        }}}
        reg = SoftwareStopRegistry.from_config(
            cfg, broker_name="alpaca", repo_root=tmp_path,
        )
        assert reg is not None
        assert reg.path == (tmp_path / "data" / "rq105"
                            / "software_stops.alpaca.json")
        # Idempotent tagging + sim/test passthrough.
        assert registry_path_for(reg.path, "alpaca") == reg.path
        assert registry_path_for("x/software_stops.json", None) == Path(
            "x/software_stops.json")


# ═════════════════════════════════════════════════════════════════════════════
# Never-loosen: ratchet-only invariant
# ═════════════════════════════════════════════════════════════════════════════

class TestRatchetOnlyInvariant:
    def test_lower_stop_refused(self, tmp_path, caplog):
        reg = _armed_with(tmp_path, stop=80.0)
        with caplog.at_level("WARNING", logger="live.runner"):
            reg.register("BLK", FRACTIONAL_QTY, 72.0, source="z9")
        entry = reg.get("BLK")
        assert entry["stop_price"] == 80.0             # unchanged
        assert entry["history"][-1]["action"] == "ratchet_refused"
        assert entry["history"][-1]["proposed_stop_price"] == 72.0
        assert "never-loosen" in caplog.text
        # And it survives a reload — the refusal was persisted as history,
        # not applied to the stop.
        assert SoftwareStopRegistry(reg.path).get("BLK")["stop_price"] == 80.0

    def test_higher_stop_ratchets_up(self, tmp_path):
        reg = _armed_with(tmp_path, stop=80.0)
        reg.register("BLK", FRACTIONAL_QTY, 96.0, source="z9")
        entry = reg.get("BLK")
        assert entry["stop_price"] == 96.0
        assert entry["history"][-1]["action"] == "ratchet_up"

    def test_loosening_requires_explicit_rewrite_with_reason(self, tmp_path, caplog):
        reg = _armed_with(tmp_path, stop=80.0)
        with pytest.raises(ValueError, match="reason"):
            reg.rewrite_stop("BLK", 60.0, reason="")
        with pytest.raises(ValueError, match="reason"):
            reg.rewrite_stop("BLK", 60.0, reason="   ")
        assert reg.get("BLK")["stop_price"] == 80.0

        with caplog.at_level("WARNING", logger="live.runner"):
            reg.rewrite_stop(
                "BLK", 60.0, reason="operator: post-earnings vol reset",
            )
        entry = reg.get("BLK")
        assert entry["stop_price"] == 60.0
        last = entry["history"][-1]
        assert last["action"] == "explicit_rewrite"
        assert last["previous_stop_price"] == 80.0
        assert last["reason"] == "operator: post-earnings vol reset"
        assert "explicit rewrite" in caplog.text

    def test_rewrite_unknown_symbol_raises(self, tmp_path):
        reg = _registry(tmp_path)
        with pytest.raises(KeyError):
            reg.rewrite_stop("GHOST", 10.0, reason="typo")

    def test_topup_refreshes_qty_but_not_stop_direction(self, tmp_path):
        reg = _armed_with(tmp_path, qty=0.4, stop=80.0)
        reg.register("BLK", 0.9, 80.0, source="z9")    # top-up, same stop
        entry = reg.get("BLK")
        assert entry["qty"] == 0.9                     # protected qty grows
        assert entry["stop_price"] == 80.0
        assert entry["history"][-1]["action"] == "refresh"


# ═════════════════════════════════════════════════════════════════════════════
# Trigger correctness (fractional qty)
# ═════════════════════════════════════════════════════════════════════════════

class TestTriggerCorrectness:
    def test_breach_fires_full_fractional_qty(self, tmp_path):
        reg = _armed_with(tmp_path, qty=FRACTIONAL_QTY, stop=760.0)
        intents = reg.evaluate({"BLK": 760.0})         # price == stop -> fires
        assert len(intents) == 1
        intent = intents[0]
        assert intent["symbol"] == "BLK"
        assert intent["qty"] == FRACTIONAL_QTY          # FULL registered qty
        assert intent["stop_price"] == 760.0
        assert intent["trigger_price"] == 760.0
        assert intent["gap_pct"] == 0.0
        assert "software_stop breach" in intent["reason"]

    def test_above_stop_does_not_fire(self, tmp_path):
        reg = _armed_with(tmp_path, stop=760.0)
        assert reg.evaluate({"BLK": 760.01}) == []

    def test_missing_or_bad_quote_stays_armed(self, tmp_path, caplog):
        reg = _armed_with(tmp_path, stop=760.0)
        with caplog.at_level("WARNING", logger="live.runner"):
            assert reg.evaluate({}) == []
            assert reg.evaluate({"BLK": float("nan")}) == []
        assert "NOT evaluated" in caplog.text
        assert reg.get("BLK") is not None              # still protected

    def test_refires_until_deregistered(self, tmp_path):
        """A breached stop stays registered until the exit is broker-
        confirmed (commit deregisters on full liquidation) — a failed
        SELL re-fires next pass instead of silently unprotecting."""
        reg = _armed_with(tmp_path, stop=760.0)
        assert len(reg.evaluate({"BLK": 700.0})) == 1
        assert len(reg.evaluate({"BLK": 700.0})) == 1
        reg.deregister("BLK", reason="full liquidation")
        assert reg.evaluate({"BLK": 700.0}) == []


# ═════════════════════════════════════════════════════════════════════════════
# Gap-down-through-stop (design §3.3)
# ═════════════════════════════════════════════════════════════════════════════

class TestGapThroughPricing:
    def test_gap_size_measured_logged_and_carried(self, tmp_path, caplog):
        reg = _armed_with(tmp_path, stop=760.0)
        with caplog.at_level("WARNING", logger="live.runner"):
            intents = reg.evaluate({"BLK": 700.0})     # gapped 7.89% through
        assert len(intents) == 1
        intent = intents[0]
        assert intent["trigger_price"] == 700.0
        assert intent["gap_pct"] == pytest.approx((760.0 - 700.0) / 760.0)
        assert "gap" in intent["reason"]
        # Slippage accepted + logged with the gap size (§3.3).
        assert "gap=7.89%" in caplog.text
        assert "slippage accepted" in caplog.text

    def test_exit_intent_is_market_exit_for_full_qty_regardless_of_gap(
            self, tmp_path):
        reg = _armed_with(tmp_path, qty=FRACTIONAL_QTY, stop=760.0)
        deep = reg.evaluate({"BLK": 380.0})            # 50% through the stop
        assert deep[0]["qty"] == FRACTIONAL_QTY
        assert deep[0]["gap_pct"] == pytest.approx(0.5)


# ═════════════════════════════════════════════════════════════════════════════
# Corruption fail-closed (blocks NEW fractional entries, never silent)
#
# Note: the stage-0-capability-gate integration test (a corrupt registry
# blocking a real RunnerAdapter.commit) stays in the umbrella's own test
# file — it exercises umbrella-only orchestration (adapters.commit_contract,
# FakeBroker, RunnerAdapter), not this registry in isolation.
# ═════════════════════════════════════════════════════════════════════════════

class TestCorruptRegistryFailClosed:
    def _corrupt_registry(self, tmp_path, payload="{ not json"):
        path = tmp_path / "software_stops.json"
        path.write_text(payload)
        return SoftwareStopRegistry(path), path

    def test_corrupt_is_not_armed(self, tmp_path):
        reg, _ = self._corrupt_registry(tmp_path)
        assert reg.corrupt is True
        assert reg.is_armed() is False

    def test_schema_violation_is_corrupt(self, tmp_path):
        bad = {"version": 1, "stops": {"BLK": {
            "symbol": "BLK", "qty": -1, "stop_price": 80.0, "source": "z9",
        }}}
        reg, _ = self._corrupt_registry(tmp_path, json.dumps(bad))
        assert reg.is_armed() is False

    def test_corrupt_refuses_writes_and_preserves_bytes(self, tmp_path, caplog):
        reg, path = self._corrupt_registry(tmp_path)
        original = path.read_bytes()
        with pytest.raises(SoftwareStopRegistryCorrupt):
            reg.register("BLK", 1.0, 80.0, source="z9")
        with pytest.raises(SoftwareStopRegistryCorrupt):
            reg.rewrite_stop("BLK", 60.0, reason="attempt")
        with caplog.at_level("ERROR", logger="live.runner"):
            assert reg.evaluate({"BLK": 1.0}) == []    # loud, no exits invented
        assert "CORRUPT" in caplog.text
        assert path.read_bytes() == original           # evidence untouched

    def test_corrupt_watchdog_state(self, tmp_path):
        reg, _ = self._corrupt_registry(tmp_path)
        state = reg.staleness_state(now=NOW)
        assert state["corrupt"] is True
        assert state["stale"] is True


# ═════════════════════════════════════════════════════════════════════════════
# Staleness watchdog (design §3.4)
#
# Note: the ops watchdog CLI script's own exit-code tests
# (scripts/check_software_stops_liveness.py) stay in the umbrella — this
# file only carries the pure compute_staleness arithmetic + registry
# heartbeat stamping this repo actually owns.
# ═════════════════════════════════════════════════════════════════════════════

class TestStalenessWatchdog:
    def _snapshot(self, *, n_stops=1, heartbeat, budget=30.0):
        stops = {
            f"S{i}": {"symbol": f"S{i}", "qty": 0.5, "stop_price": 10.0,
                      "armed_at": "2026-07-03", "source": "z9", "history": []}
            for i in range(n_stops)
        }
        return {
            "version": 1, "contract": "software-stops-v1",
            "max_staleness_minutes": budget,
            "last_evaluated_at": heartbeat, "stops": stops,
        }

    def test_arithmetic(self):
        fresh = (NOW - datetime.timedelta(minutes=10)).isoformat()
        old = (NOW - datetime.timedelta(minutes=31)).isoformat()

        s = compute_staleness(self._snapshot(heartbeat=fresh), now=NOW)
        assert s["stale"] is False
        assert s["age_minutes"] == pytest.approx(10.0)

        s = compute_staleness(self._snapshot(heartbeat=old), now=NOW)
        assert s["stale"] is True
        assert s["age_minutes"] == pytest.approx(31.0)

        # Armed entries with NO heartbeat ever -> stale.
        s = compute_staleness(self._snapshot(heartbeat=None), now=NOW)
        assert s["stale"] is True

        # No armed entries -> never stale (nothing unprotected).
        s = compute_staleness(
            self._snapshot(n_stops=0, heartbeat=old), now=NOW)
        assert s["stale"] is False

        # No registry file at all -> ok.
        s = compute_staleness(None, now=NOW)
        assert s["exists"] is False
        assert s["stale"] is False

        # Budget honored from the file.
        s = compute_staleness(
            self._snapshot(heartbeat=old, budget=60.0), now=NOW)
        assert s["stale"] is False

    def test_default_budget_matches_loop_cadence(self):
        # 12-minute loop cadence x 2 missed passes + slack.
        assert DEFAULT_MAX_STALENESS_MINUTES == 30.0

    def test_evaluate_stamps_fresh_heartbeat(self, tmp_path):
        reg = _armed_with(tmp_path)
        reg.evaluate({"BLK": 900.0}, now=NOW)
        state = reg.staleness_state(now=NOW)
        assert state["stale"] is False
        assert state["age_minutes"] == pytest.approx(0.0, abs=1e-6)


# ═════════════════════════════════════════════════════════════════════════════
# Public schema contract (software-stops-v1) — Codex review on
# renquant-execution#30 / renquant-orchestrator#481 (2026-07-12T11:57:53Z):
# the cross-repo consumer (renquant-execution's software_stops_liveness
# checker) must depend on an EXPLICIT public pipeline name, not the private
# ``_validate_snapshot``. These tests exercise ``validate_software_stop_snapshot``
# directly and prove it is a genuine thin wrapper (not a divergent
# reimplementation) by comparing its behavior against ``_validate_snapshot``
# for both a valid and an invalid input.
# ═════════════════════════════════════════════════════════════════════════════

class TestPublicValidateSoftwareStopSnapshot:
    def _valid_one_stop(self):
        return {
            "version": 1,
            "stops": {
                "BLK": {
                    "symbol": "BLK",
                    "qty": 0.435578,
                    "stop_price": 760.0,
                    "source": "z9",
                },
            },
        }

    def test_valid_empty_registry(self):
        raw = {"version": 1, "stops": {}}
        assert validate_software_stop_snapshot(raw) == raw

    def test_valid_one_stop_entry(self):
        raw = self._valid_one_stop()
        assert validate_software_stop_snapshot(raw) == raw

    def test_thin_wrapper_identity_valid_case(self):
        # Same object back, same result as the private implementation --
        # proves this is a wrapper, not a divergent reimplementation.
        raw = self._valid_one_stop()
        assert validate_software_stop_snapshot(raw) == _validate_snapshot(raw)
        assert validate_software_stop_snapshot(raw) is raw

    def test_thin_wrapper_identity_invalid_case(self):
        bad = {"version": 999, "stops": {}}
        with pytest.raises(ValueError):
            validate_software_stop_snapshot(bad)
        with pytest.raises(ValueError):
            _validate_snapshot(bad)

    def test_root_not_a_dict(self):
        with pytest.raises(ValueError, match="registry root must be an object"):
            validate_software_stop_snapshot(["not", "a", "dict"])

    def test_wrong_version(self):
        raw = {"version": 2, "stops": {}}
        with pytest.raises(ValueError, match="unsupported registry version"):
            validate_software_stop_snapshot(raw)

    def test_stops_not_a_dict(self):
        raw = {"version": 1, "stops": "not-a-dict"}
        with pytest.raises(ValueError, match="registry 'stops' must be an object"):
            validate_software_stop_snapshot(raw)

    def test_symbol_mismatch(self):
        raw = {
            "version": 1,
            "stops": {"BLK": {"symbol": "WRONG", "qty": 1.0,
                               "stop_price": 80.0, "source": "z9"}},
        }
        with pytest.raises(ValueError, match="symbol mismatch"):
            validate_software_stop_snapshot(raw)

    def test_invalid_qty(self):
        raw = {
            "version": 1,
            "stops": {"BLK": {"symbol": "BLK", "qty": -1,
                               "stop_price": 80.0, "source": "z9"}},
        }
        with pytest.raises(ValueError, match="qty invalid"):
            validate_software_stop_snapshot(raw)

    def test_invalid_stop_price(self):
        raw = {
            "version": 1,
            "stops": {"BLK": {"symbol": "BLK", "qty": 1.0,
                               "stop_price": 0.0, "source": "z9"}},
        }
        with pytest.raises(ValueError, match="stop_price invalid"):
            validate_software_stop_snapshot(raw)

    def test_invalid_source(self):
        raw = {
            "version": 1,
            "stops": {"BLK": {"symbol": "BLK", "qty": 1.0,
                               "stop_price": 80.0, "source": "not-a-source"}},
        }
        with pytest.raises(ValueError, match="source invalid"):
            validate_software_stop_snapshot(raw)

    def test_invalid_max_staleness_minutes(self):
        raw = {"version": 1, "stops": {}, "max_staleness_minutes": -5.0}
        with pytest.raises(ValueError, match="max_staleness_minutes invalid"):
            validate_software_stop_snapshot(raw)
