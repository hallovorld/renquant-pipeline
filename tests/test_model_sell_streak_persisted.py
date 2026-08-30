"""model_sell streak increments at most once per session date ACROSS RUNS.

2026-08-25: two runs on one date (f184d281, bbd3a0f9) each incremented the
streak because ``sell_streaks`` was persisted but ``last_streak_inc_date`` —
the once-per-day dedup key in ``exits.check_model_sell`` — was not (0 refs
in live_state_v2). The streak reached 3 at 06:30 08-26 → model_sell after
two sessions. ``HoldingV2.last_streak_inc_date`` (wire key
``last_streak_inc_dates``) now round-trips it.
"""
from __future__ import annotations

import datetime
import json

from renquant_pipeline.kernel.exits import HoldingState, check_model_sell
from renquant_pipeline.kernel.live_state_v2 import (
    HoldingV2,
    LiveStateV2,
    streak_inc_date_from_wire,
    streak_inc_date_to_wire,
)

TUE = datetime.date(2026, 8, 25)
WED = datetime.date(2026, 8, 26)
REQUIRED = 3


def _persist(hs: HoldingState) -> dict:
    """What a runner writes at end of run (through the one authority)."""
    state = LiveStateV2(holdings={"CRWD": HoldingV2(
        entry_date=hs.entry_date.isoformat(),
        sell_streak=hs.sell_streak,
        last_streak_inc_date=streak_inc_date_to_wire(hs.last_streak_inc_date),
    )})
    return json.loads(state.canonical_json())


def _restore(wire: dict, *, with_date: bool = True) -> HoldingState:
    """What a runner rebuilds at start of run."""
    h = LiveStateV2.parse(wire).holdings["CRWD"]
    return HoldingState(
        entry_price=100.0, entry_date=datetime.date.fromisoformat(h.entry_date),
        high_watermark=100.0, sell_streak=h.sell_streak,
        last_streak_inc_date=(streak_inc_date_from_wire(h.last_streak_inc_date)
                              if with_date else None),
    )


def _run(hs: HoldingState, today: datetime.date):
    return check_model_sell("sell", hs, REQUIRED, 0, today)


def test_two_runs_same_date_increment_once_then_next_date_once():
    hs = HoldingState(entry_price=100.0, entry_date=datetime.date(2026, 8, 1),
                      high_watermark=100.0, sell_streak=1)
    # run 1 (08-25 early)
    hs, sig = _run(hs, TUE)
    assert hs.sell_streak == 2 and hs.last_streak_inc_date == TUE and not sig.should_exit
    wire = _persist(hs)
    assert wire["last_streak_inc_dates"] == {"CRWD": "2026-08-25"}
    assert wire["sell_streaks"] == {"CRWD": 2}
    # run 2 (08-25 later, e.g. the post-close rerun)
    hs2, sig = _run(_restore(wire), TUE)
    assert hs2.sell_streak == 2 and not sig.should_exit, "same session: +1 at most once"
    wire = _persist(hs2)
    # run 3 (08-26): a genuine third consecutive sell → fires
    hs3, sig = _run(_restore(wire), WED)
    assert hs3.sell_streak == 3 and sig.should_exit
    assert hs3.last_streak_inc_date == WED


def test_pre_fix_shape_without_the_date_double_counts():
    """Guard-the-guard: dropping the restored date reproduces the 08-25 bug."""
    hs = HoldingState(entry_price=100.0, entry_date=datetime.date(2026, 8, 1),
                      high_watermark=100.0, sell_streak=1)
    hs, _ = _run(hs, TUE)
    wire = _persist(hs)
    hs2, sig = _run(_restore(wire, with_date=False), TUE)
    assert hs2.sell_streak == 3 and sig.should_exit   # what production did


def test_field_round_trips_and_defaults():
    h = HoldingV2(entry_date="2026-08-01", sell_streak=2,
                  last_streak_inc_date="2026-08-25")
    s = LiveStateV2(holdings={"CRWD": h})
    wire = json.loads(s.canonical_json())
    assert wire["last_streak_inc_dates"] == {"CRWD": "2026-08-25"}
    assert LiveStateV2.parse(wire) == s
    # v1 file without the key → None (never incremented), no quarantine
    old = {"entry_dates": {"CRWD": "2026-08-01"}, "sell_streaks": {"CRWD": 2}}
    parsed = LiveStateV2.parse(old)
    assert parsed.holdings["CRWD"].last_streak_inc_date is None
    assert parsed.extra_quarantine == {}
    # a None date is omitted from the collection (v1 readers iterate it)
    assert parsed.to_wire()["last_streak_inc_dates"] == {}


def test_wire_helpers():
    assert streak_inc_date_from_wire(None) is None
    assert streak_inc_date_from_wire("") is None
    assert streak_inc_date_from_wire("2026-08-25") == TUE
    assert streak_inc_date_from_wire("2026-08-25T13:55:00") == TUE
    assert streak_inc_date_from_wire("garbage") is None
    assert streak_inc_date_from_wire(TUE) == TUE
    assert streak_inc_date_to_wire(None) is None
    assert streak_inc_date_to_wire(TUE) == "2026-08-25"
    assert streak_inc_date_to_wire(datetime.datetime(2026, 8, 25, 13, 55)) == "2026-08-25"
