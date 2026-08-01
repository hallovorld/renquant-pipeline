"""A task-level skip must NAME a lane, or its own consumer discards the record.

Measured 2026-07-31 against `rq104_shadow_scorer_sentinel.is_valid_v1_record` on the live
`shadow_scorer_health.jsonl`: 12 `degraded` rows parsed, **4 `no_shadow_models` rows
returned False and were ignored entirely**. Their `shadow_name` was `None`, and the
consumer requires a `str`.

So the `expected_skip` status this module carefully defines had never been exercised for
those rows — the producer emitted a record its own consumer refuses by definition. The
consumer is right to demand attribution; the fix is here.
"""

from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    STATE_DISABLED,
    STATE_NO_SHADOW_MODELS,
    TASK_LEVEL_SHADOW_NAME,
    mark_expected_skip,
    new_shadow_health,
)


def _task_level_record(state: str, reason: str) -> dict:
    """Mirrors `shadow_scoring._skip_record({}, ...)` — the task-level path."""
    sm: dict = {}
    rec = new_shadow_health(
        shadow_name=(sm.get("name", "unnamed_shadow") if sm else TASK_LEVEL_SHADOW_NAME),
        kind=(sm.get("kind") if sm else None),
        artifact_path=(sm.get("artifact_path") if sm else None),
        run_date=dt.date(2026, 7, 31), run_id="r1", n_candidates=0,
        expected_content_sha256=None, expected_config_fingerprint=None,
    )
    return mark_expected_skip(rec, state, reason)


def test_the_sentinel_name_is_a_string_not_None():
    """The single property the consumer's parser requires."""
    for state in (STATE_NO_SHADOW_MODELS, STATE_DISABLED):
        rec = _task_level_record(state, "reason")
        assert isinstance(rec["shadow_name"], str), rec["shadow_name"]
        assert rec["shadow_name"] == TASK_LEVEL_SHADOW_NAME


def test_it_is_unmistakably_a_SENTINEL_not_a_plausible_lane_name():
    """A real lane is named in config. If this looked like one, a reader would count it
    as a configured shadow that is permanently skipping."""
    assert TASK_LEVEL_SHADOW_NAME.startswith("__")
    assert TASK_LEVEL_SHADOW_NAME.endswith("__")
    assert "shadow" not in TASK_LEVEL_SHADOW_NAME.replace("__", "")


def test_the_record_is_still_an_EXPECTED_SKIP_not_a_fault():
    """Naming the lane must not turn a by-design non-run into an alarm.

    The consumer alarms on `status == "fault"`. If this change made these records
    parseable AND faulty, it would manufacture a daily alarm out of a deliberate
    configuration — the opposite of the intent.
    """
    rec = _task_level_record(STATE_NO_SHADOW_MODELS, "no shadow_models configured")
    assert rec["status"] == "expected_skip"
    assert rec["state"] == STATE_NO_SHADOW_MODELS
    assert rec["loaded"] is False
    assert rec["n_scored"] == 0


def test_a_REAL_lane_is_unaffected():
    """The change must touch only the task-level path."""
    sm = {"name": "topdecile_clf_blend_leg", "kind": "xgb"}
    rec = new_shadow_health(
        shadow_name=(sm.get("name", "unnamed_shadow") if sm else TASK_LEVEL_SHADOW_NAME),
        kind=sm.get("kind"), artifact_path=None,
        run_date=dt.date(2026, 7, 31), run_id="r1", n_candidates=3,
        expected_content_sha256=None, expected_config_fingerprint=None,
    )
    assert rec["shadow_name"] == "topdecile_clf_blend_leg"


def test_the_producer_invariant_actionable_equals_not_fault_still_holds():
    """`actionable == (status != "fault")` is the documented contract; naming the lane
    must not perturb it. (Note the field means "serviceable", not "act on this".)"""
    rec = _task_level_record(STATE_DISABLED, "shadow_enabled=false")
    assert rec["actionable"] == (rec["status"] != "fault")
