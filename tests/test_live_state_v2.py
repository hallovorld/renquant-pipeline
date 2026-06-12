"""LiveStateV2 errata-D acceptance matrix.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.4 /
S1-PR1, errata D ("adding a field requires green on" exactly this matrix):

  1. v1→v2 migration against a golden v1 fixture
  2. unknown-field policy: quarantine, never silent drop
  3. rollback-read: old code reading new state must not corrupt
  4. atomic write: tmp+rename with crash injection
  5. DB-snapshot parity: JSON ⇄ live_state_snapshots round-trip
  6. round-trip property: parse(serialize(s)) == s over generated states

The golden fixture mirrors the real production live_state.alpaca.json
shape (2026-06-12), values anonymized.
"""
from __future__ import annotations

import datetime
import json
import os
import random

import pytest

from renquant_pipeline.kernel.live_state_v2 import (
    EntrySignalV2,
    HoldingV2,
    LiveStateV2,
    MonitorStateV2,
    RegimeStateV2,
    read_live_state,
    write_live_state_atomic,
)

# Golden v1 fixture — production live_state.alpaca.json shape (2026-06-12).
# Note: MU has an entry_signal, GE does not; protection_breaches absent
# entirely (the PR-#294 field) — both real conditions.
GOLDEN_V1 = {
    "regime": "BULL_CALM",
    "regime_confidence": 0.5932,
    "high_water_mark": 11079.22,
    "entry_dates": {"MU": "2026-04-27", "GE": "2026-05-14"},
    "sell_streaks": {"MU": 0, "GE": 0},
    "last_sell_dates": {"BA": "2026-05-15", "CAT": "2026-05-01"},
    "position_hwm": {"MU": 976.025, "GE": 325.76},
    "entry_signals": {
        "MU": {"rank_score": 0.325, "panel_score": None, "kelly_target_pct": None},
    },
    "last_stop_exit_dates": {},
    "skip_buys": False,
    "monitor_state": {
        "no_trade_streak": 2,
        "no_candidate_streak": 0,
        "last_activity_date": "2026-05-27",
        "first_trade_date": "2026-04-23",
        "no_trade_streak_source": "broker_filled_orders",
        "last_fill_date": "2026-05-27",
        "last_check_date": "2026-05-30",
    },
    "regime_state": {
        "regime": "BULL_CALM",
        "confidence": 0.5932,
        "in_transition": False,
        "countdown": 0,
        "cusum_pos": 0.0,
        "cusum_neg": 0.0,
        "cooldown_start": None,
    },
    "stop_orders": {},
    "recent_sell_orders": {"oid-1": {"ticker": "BA", "submitted": "2026-06-12"}},
}


# ── 1. v1→v2 migration (golden fixture) ─────────────────────────────────

class TestV1Migration:

    def test_golden_parse(self):
        s = LiveStateV2.parse(GOLDEN_V1)
        assert set(s.holdings) == {"MU", "GE"}
        mu = s.holdings["MU"]
        assert mu.entry_date == "2026-04-27"
        assert mu.position_hwm == 976.025
        assert mu.entry_signal == EntrySignalV2(rank_score=0.325)
        # PR-#294 field absent in v1 → schema default, not KeyError
        assert mu.protection_breaches == 0
        assert s.holdings["GE"].entry_signal is None
        assert isinstance(s.monitor_state, MonitorStateV2)
        assert s.monitor_state.no_trade_streak == 2
        assert isinstance(s.regime_state, RegimeStateV2)
        assert s.regime_state.cooldown_start is None
        assert s.extra_quarantine == {}
        assert s.recent_sell_orders == {"oid-1": {"ticker": "BA", "submitted": "2026-06-12"}}

    def test_empty_state_parses_to_defaults(self):
        s = LiveStateV2.parse({})
        assert s.regime == "UNKNOWN"
        assert s.holdings == {}
        assert s.monitor_state is None
        assert s.skip_buys is False

    def test_v2_stamped_wire_reparses(self):
        s = LiveStateV2.parse(GOLDEN_V1)
        again = LiveStateV2.parse(s.to_wire())
        assert again == s


# ── 2. unknown-field policy: quarantine, never silent drop ──────────────

class TestUnknownFieldQuarantine:

    def test_unknown_key_quarantined_and_reemitted(self):
        raw = dict(GOLDEN_V1)
        raw["future_field_from_newer_writer"] = {"x": 1}
        s = LiveStateV2.parse(raw)
        assert s.extra_quarantine == {"future_field_from_newer_writer": {"x": 1}}
        # A rewrite by THIS (older) code must not lose the newer writer's key.
        assert s.to_wire()["future_field_from_newer_writer"] == {"x": 1}

    def test_typed_model_forbids_unknown_fields(self):
        # extra="forbid" everywhere: unknown fields can only enter via the
        # quarantine, never silently into the typed model.
        with pytest.raises(Exception):
            HoldingV2(entry_date="2026-01-01", surprise=1)
        with pytest.raises(Exception):
            LiveStateV2(surprise=1)


# ── 3. rollback-read: old code reading new state must not corrupt ───────

class TestRollbackRead:

    def test_wire_is_v1_flat(self):
        wire = LiveStateV2.parse(GOLDEN_V1).to_wire()
        # Exactly the access patterns v1 runner code uses today:
        assert wire["entry_dates"] == GOLDEN_V1["entry_dates"]
        assert wire["sell_streaks"] == GOLDEN_V1["sell_streaks"]
        assert wire["position_hwm"] == GOLDEN_V1["position_hwm"]
        assert wire["last_sell_dates"] == GOLDEN_V1["last_sell_dates"]
        assert wire.get("skip_buys") is False
        assert wire["monitor_state"] == GOLDEN_V1["monitor_state"]
        assert wire["regime_state"] == GOLDEN_V1["regime_state"]
        # v1 readers .get() unknown keys → the stamp is additive, harmless
        assert wire["schema_version"] == 2
        # no typed-model artifacts leak onto the wire
        assert "holdings" not in wire
        assert "extra_quarantine" not in wire

    def test_v1_rewrite_simulation(self):
        # Old code typically mutates the flat dicts then re-dumps json.
        wire = LiveStateV2.parse(GOLDEN_V1).to_wire()
        wire["entry_dates"]["NEW"] = "2026-06-12"
        wire["sell_streaks"]["NEW"] = 0
        reparsed = LiveStateV2.parse(json.loads(json.dumps(wire)))
        assert "NEW" in reparsed.holdings
        assert reparsed.holdings["MU"].entry_date == "2026-04-27"


# ── 4. atomic write with crash injection ────────────────────────────────

class TestAtomicWrite:

    def test_write_and_read_back(self, tmp_path):
        p = tmp_path / "live_state.alpaca.json"
        s = LiveStateV2.parse(GOLDEN_V1)
        write_live_state_atomic(p, s)
        assert read_live_state(p) == s

    def test_crash_before_rename_leaves_old_state_intact(self, tmp_path, monkeypatch):
        p = tmp_path / "live_state.alpaca.json"
        s1 = LiveStateV2.parse(GOLDEN_V1)
        write_live_state_atomic(p, s1)

        def _crash(src, dst):
            raise OSError("simulated crash before rename")

        monkeypatch.setattr(os, "replace", _crash)
        s2 = s1.model_copy(update={"skip_buys": True})
        with pytest.raises(OSError, match="simulated crash"):
            write_live_state_atomic(p, s2)
        monkeypatch.undo()
        # Old state fully intact, no partial write, no tmp litter.
        assert read_live_state(p) == s1
        assert list(tmp_path.glob("*.tmp")) == []

    def test_no_target_file_until_complete(self, tmp_path, monkeypatch):
        p = tmp_path / "live_state.alpaca.json"

        def _crash(src, dst):
            raise OSError("simulated crash")

        monkeypatch.setattr(os, "replace", _crash)
        with pytest.raises(OSError):
            write_live_state_atomic(p, LiveStateV2.parse(GOLDEN_V1))
        assert not p.exists()


# ── 5. DB-snapshot parity (JSON ⇄ live_state_snapshots) ─────────────────

class TestDbSnapshotParity:

    def test_snapshot_round_trip(self, tmp_path):
        from renquant_pipeline.kernel.persistence import (
            get_connection,
            load_latest_live_state,
            record_live_state_snapshot,
        )

        config = {"persistence": {"enabled": True,
                                  "db_path": str(tmp_path / "runs.db")}}
        conn = get_connection(config)
        assert conn is not None
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, run_date, run_type, strategy)"
            " VALUES ('r1', '2026-06-12', 'full', 'renquant_104')")
        s = LiveStateV2.parse(GOLDEN_V1)
        record_live_state_snapshot(
            conn, "r1", run_date=datetime.date(2026, 6, 12),
            strategy="renquant_104", state=s.to_wire(),
        )
        blob = load_latest_live_state(conn, strategy="renquant_104")
        assert blob is not None
        assert LiveStateV2.parse(blob) == s
        conn.close()


# ── 6. round-trip property: parse(serialize(s)) == s ────────────────────

def _random_state(rng: random.Random) -> LiveStateV2:
    tickers = rng.sample(["AAPL", "MU", "GE", "META", "HON", "EQIX", "NVDA"],
                         rng.randint(0, 5))
    holdings = {}
    for t in tickers:
        holdings[t] = HoldingV2(
            entry_date=f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            sell_streak=rng.randint(0, 5),
            protection_breaches=rng.randint(0, 3),
            position_hwm=rng.choice([None, round(rng.uniform(10, 2000), 3)]),
            entry_signal=rng.choice([
                None,
                EntrySignalV2(rank_score=round(rng.uniform(-1, 1), 6),
                              regime=rng.choice([None, "BULL_CALM", "BEAR"])),
            ]),
        )
    return LiveStateV2(
        regime=rng.choice(["BULL_CALM", "BULL_VOLATILE", "BEAR", "UNKNOWN"]),
        regime_confidence=round(rng.uniform(0, 1), 4),
        high_water_mark=rng.choice([None, round(rng.uniform(5000, 20000), 2)]),
        holdings=holdings,
        last_sell_dates={t: "2026-05-01" for t in tickers[:2]},
        last_stop_exit_dates={},
        skip_buys=rng.random() < 0.5,
        monitor_state=rng.choice([None, MonitorStateV2(no_trade_streak=rng.randint(0, 9))]),
        regime_state=rng.choice([None, RegimeStateV2(regime="BEAR", confidence=0.7)]),
        stop_orders=rng.choice([{}, {"MU": {"order_id": "abc", "stop_price": 70.5}}]),
        recent_sell_orders=rng.choice([{}, {"oid": {"ticker": "BA"}}]),
        extra_quarantine=rng.choice([{}, {"some_foreign_key": [1, 2]}]),
    )


class TestRoundTripProperty:
    # hypothesis is not in the project env; a seeded exhaustive-ish sweep
    # keeps the property deterministic and dependency-free. If hypothesis
    # is added later, this becomes @given(states()).

    def test_parse_serialize_identity_200_cases(self):
        rng = random.Random(44)
        for i in range(200):
            s = _random_state(rng)
            wire = json.loads(json.dumps(s.to_wire()))  # force JSON round-trip
            again = LiveStateV2.parse(wire)
            assert again == s, f"case {i} diverged"

    def test_golden_identity(self):
        s = LiveStateV2.parse(GOLDEN_V1)
        assert LiveStateV2.parse(json.loads(s.canonical_json())) == s
