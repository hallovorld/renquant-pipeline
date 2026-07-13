"""Unit tests for the versioned pre-quantization sizing-intent contract."""
from __future__ import annotations

import pytest

from renquant_pipeline.sizing_intent import (
    SIZING_INTENT_KIND,
    SIZING_INTENT_SCHEMA_VERSION,
    SizingIntentContractError,
    SizingIntentRecord,
    parse_sizing_intent_record,
)


_SHA = "a" * 64


def _record(**overrides) -> SizingIntentRecord:
    values = {
        "run_id": "run-104-20260712",
        "session_id": "2026-07-12",
        "arm_id": "floor-off",
        "input_manifest_sha256": _SHA,
        "config_sha256": _SHA,
        "source": "pipeline.selection.greedy",
        "ticker": "BLK",
        "candidate_id": "2026-07-12:BLK:2",
        "candidate_rank": 2,
        "admission_passed": True,
        "admission_gate_outcomes": {
            "data_available": True,
            "rank_eligible": True,
            "risk_eligible": True,
        },
        "target_notional": 400.0,
        "unrounded_quantity": 0.4,
        "planned_quantity": 0.0,
        "reference_price": 1000.0,
        "reference_price_as_of": "2026-07-12T14:35:00Z",
        "per_name_cap_notional": 1200.0,
        "cash_reserve_notional": 0.0,
        "available_cash_before": 5000.0,
        "normal_buy_reservation_notional": 0.0,
        "cumulative_exposure_before_notional": 18000.0,
        "cumulative_exposure_after_notional": 18000.0,
        "ordinary_buy_displacement_count": 0,
        "outcome": "zero_quantity_after_whole_share_floor",
        "reason": "zero_quantity_after_whole_share_floor",
    }
    values.update(overrides)
    return SizingIntentRecord(**values)


def test_zero_quantity_record_round_trips_with_schema() -> None:
    record = _record()
    payload = record.to_dict()
    assert payload["schema_version"] == SIZING_INTENT_SCHEMA_VERSION
    assert payload["kind"] == SIZING_INTENT_KIND
    assert parse_sizing_intent_record(payload) == record


def test_floor_rescue_may_exceed_target_but_not_hard_cap_or_cash() -> None:
    record = _record(
        arm_id="floor-on",
        planned_quantity=1.0,
        cumulative_exposure_after_notional=19000.0,
        outcome="emitted",
        reason=None,
    )
    assert record.planned_quantity == 1.0
    assert record.planned_quantity * record.reference_price > record.target_notional
    assert record.ordinary_buy_displacement_count == 0


def test_gate_level_admission_evidence_must_match_summary() -> None:
    with pytest.raises(SizingIntentContractError, match="conjunction"):
        _record(admission_gate_outcomes={"data_available": False})


def test_exposure_after_must_include_the_planned_notional() -> None:
    with pytest.raises(SizingIntentContractError, match="cumulative_exposure_after"):
        _record(
            planned_quantity=1.0,
            outcome="emitted",
            reason=None,
        )


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"input_manifest_sha256": "not-a-hash"}, "sha256"),
        ({"run_id": None}, "run_id"),
        ({"candidate_id": ""}, "candidate_id"),
        ({"candidate_rank": True}, "candidate_rank"),
        ({"admission_gate_outcomes": {"rank_eligible": "yes"}}, "values"),
        ({"ordinary_buy_displacement_count": -1}, "displacement"),
        ({"target_notional": 401.0}, "unrounded_quantity"),
        ({"planned_quantity": 2.0, "outcome": "emitted", "reason": None}, "cap"),
        ({"planned_quantity": 1.0, "available_cash_before": 900.0,
          "outcome": "emitted", "reason": None}, "cash"),
        ({"outcome": "blocked", "reason": None}, "reason"),
    ],
)
def test_invalid_records_fail_closed(override, match: str) -> None:
    with pytest.raises(SizingIntentContractError, match=match):
        _record(**override)


def test_unknown_schema_fails_closed() -> None:
    payload = _record().to_dict()
    payload["schema_version"] = "future-schema"
    with pytest.raises(SizingIntentContractError, match="schema_version"):
        parse_sizing_intent_record(payload)


def test_unknown_record_field_fails_closed() -> None:
    payload = _record().to_dict()
    payload["unreviewed_extension"] = True
    with pytest.raises(SizingIntentContractError, match="unknown fields"):
        parse_sizing_intent_record(payload)
