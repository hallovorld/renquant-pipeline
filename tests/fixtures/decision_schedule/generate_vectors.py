"""Regenerate ``vectors.json`` — the decision-schedule verdict fixtures.

G4 re-registration implementation step 1 (renquant-model#61 v4 §5):
these vectors pin the verdict semantics of
``renquant_pipeline.decision_schedule.validate_session_records`` for
every consumer of the §2 next-open observation contract (orchestrator
canonical job — step 2; model score backfill / PIT ledger — step 3).
Promoting this FILE to renquant-common (so sibling repos test the same
vectors without importing pipeline test code) is a later step, mirroring
the bundle_contract precedent — keep it self-contained and
JSON-serializable for that move.

Digest basis: artifact/config/decision digests are computed by the
repo's REAL hashing utility ``renquant_artifacts.contracts.hash_jsonable``
over the payload stubs below, and job ids by
``renquant_pipeline.decision_schedule.job_identity`` (whose canonical
form is itself pinned byte-equal to ``hash_jsonable`` by the test
suite) — the vectors pin against the shared implementations, never a
local re-fork.

Run from the repo root (test env on PYTHONPATH):

    python tests/fixtures/decision_schedule/generate_vectors.py
"""
from __future__ import annotations

import json
from pathlib import Path

from renquant_artifacts.contracts import hash_jsonable

from renquant_pipeline.decision_schedule import job_identity

OUT = Path(__file__).parent / "vectors.json"

#: One frozen session for every case: T = 2026-07-16 (NYSE), close(T) =
#: 16:00 ET = 20:00 UTC, first regular-session open after T =
#: 2026-07-17 09:30 ET = 13:30 UTC. Resolution of these instants from
#: ``calendar_id`` is the orchestrator's job (step 2); the vectors carry
#: the resolved window as frozen input.
SESSION = "2026-07-16"
SESSION_WINDOW = {
    "close": "2026-07-16T20:00:00+00:00",
    "next_open": "2026-07-17T13:30:00+00:00",
    "next_open_session": "2026-07-17",
}
CALENDAR_ID = "nyse/2026.06"
PRICE_SOURCE_ID = "alpaca-iex/v1"

#: Frozen artifact stubs per arm (v4 §3: champion = single frozen
#: artifact/config digest; L1 = the single frozen equal-weight
#: combination). Content is a deterministic stand-in; the DIGESTS are
#: real sha256 values from hash_jsonable.
ARM_ARTIFACTS = {
    "champion": {
        "scorer": {"kind": "panel_ltr_xgboost", "trained_date": "2026-06-20",
                   "note": "decision-schedule fixture champion scorer"},
        "calibrator": {"kind": "global_panel_calibration",
                       "trained_date": "2026-06-21",
                       "note": "decision-schedule fixture champion calibrator"},
    },
    "l1": {
        "ensemble": {"kind": "equal_weight_l1", "experts": ["e1", "e2", "e3"],
                     "frozen_date": "2026-06-22",
                     "note": "decision-schedule fixture L1 combination"},
    },
}
CONFIG_STUB = {"universe": "renquant-104", "top_n": 3,
               "note": "decision-schedule fixture frozen config"}

ORDERS = {
    "champion": [
        {"ticker": "AAPL", "side": "buy", "quantity": 3},
        {"ticker": "MSFT", "side": "buy", "quantity": 2},
    ],
    "l1": [
        {"ticker": "AAPL", "side": "buy", "quantity": 2},
        {"ticker": "NVDA", "side": "buy", "quantity": 1},
    ],
}

INPUT_MANIFEST = {
    "prices_daily": {
        "digest": hash_jsonable({"input": "prices_daily", "session": SESSION}),
        "max_event_time": "2026-07-16T19:59:57+00:00",
    },
    "fundamentals": {
        "digest": hash_jsonable({"input": "fundamentals", "session": SESSION}),
        "max_event_time": "2026-07-16T11:00:00+00:00",
    },
}
#: Declared watermark == recomputed max event-time over the manifest.
WATERMARK = "2026-07-16T19:59:57+00:00"


def arm_digests(arm: str) -> dict:
    return {name: hash_jsonable(payload)
            for name, payload in ARM_ARTIFACTS[arm].items()}


def make_record(arm: str, **overrides) -> dict:
    artifact_digests = arm_digests(arm)
    config_digest = hash_jsonable(CONFIG_STUB)
    orders = overrides.pop("orders", ORDERS[arm])
    record = {
        "schema_version": 1,
        "arm": arm,
        "decision_session": SESSION,
        "declared_input_watermark": WATERMARK,
        "input_manifest": INPUT_MANIFEST,
        "artifact_digests": artifact_digests,
        "config_digest": config_digest,
        "job_id": job_identity(
            arm=arm, decision_session=SESSION,
            artifact_digests=artifact_digests, config_digest=config_digest,
        ),
        "run_bundle_timestamp": "2026-07-16T22:15:00+00:00",
        "calendar_id": CALENDAR_ID,
        "price_source_id": PRICE_SOURCE_ID,
        "orders": orders,
        "orders_scheduled_for": SESSION_WINDOW["next_open_session"],
        "decision_digest": hash_jsonable({
            "arm": arm, "decision_session": SESSION, "orders": orders,
            "orders_scheduled_for": SESSION_WINDOW["next_open_session"],
        }),
        "failure": None,
    }
    record.update(overrides)
    return record


def failure_record(arm: str, kind: str, detail: str) -> dict:
    return {
        "schema_version": 1,
        "arm": arm,
        "decision_session": SESSION,
        "calendar_id": CALENDAR_ID,
        "price_source_id": PRICE_SOURCE_ID,
        "failure": {"kind": kind, "detail": detail},
    }


def case(name, description, records, expected, arm_expected) -> dict:
    return {
        "name": name,
        "description": description,
        "session_window": SESSION_WINDOW,
        "expected_calendar_id": CALENDAR_ID,
        "expected_price_source_id": PRICE_SOURCE_ID,
        "records": records,
        "expected": expected,
        "expected_arm_verdicts": arm_expected,
    }


def build_cases() -> list[dict]:
    l1 = make_record("l1")
    champion = make_record("champion")

    # Late watermark: declared AND per-input event time 5s after close —
    # isolates watermark_after_close (recompute still agrees).
    late_manifest = json.loads(json.dumps(INPUT_MANIFEST))
    late_manifest["prices_daily"]["max_event_time"] = "2026-07-16T20:00:05+00:00"
    l1_late = make_record(
        "l1",
        declared_input_watermark="2026-07-16T20:00:05+00:00",
        input_manifest=late_manifest,
    )

    # Divergent retry: same job identity (same {arm, T, artifact digests,
    # config digest}), later run-bundle timestamp, but a DIFFERENT declared
    # order set => different decision digest. Never resolved by
    # latest-commit — the later record does not win, the session fails.
    divergent_orders = [{"ticker": "AAPL", "side": "buy", "quantity": 5}]
    champion_retry_divergent = make_record(
        "champion",
        orders=divergent_orders,
        run_bundle_timestamp="2026-07-16T23:40:00+00:00",
    )

    # Byte-identical retry: identical decision/input digests, only the
    # run-bundle timestamp differs => admissible (the complement of
    # divergent_retry).
    champion_retry_identical = dict(make_record("champion"))
    champion_retry_identical["run_bundle_timestamp"] = "2026-07-16T23:40:00+00:00"

    # Timestamp outside (close(T), open(T+1)): evidence flag, not failure.
    champion_ts_outside = make_record(
        "champion", run_bundle_timestamp="2026-07-17T14:05:00+00:00",
    )

    ok_expected = {
        "ok": True, "reason_codes": [], "failure_class": None,
        "evidence_flags": [],
    }
    arm_ok = {"reason_codes": [], "failure_class": None, "evidence_flags": []}

    return [
        case(
            "valid_pair",
            "both arms qualify: watermark == recomputed max event-time <= "
            "close(T), orders for open(T+1), deterministic job identity, "
            "bundle timestamp inside (close(T), open(T+1))",
            [l1, champion],
            ok_expected,
            {"l1": arm_ok, "champion": arm_ok},
        ),
        case(
            "valid_retry_byte_identical",
            "champion retried with byte-identical decision/input digests "
            "(only the run-bundle timestamp differs) — retry is admissible",
            [l1, champion, champion_retry_identical],
            ok_expected,
            {"l1": arm_ok, "champion": arm_ok},
        ),
        case(
            "late_watermark",
            "l1 declares an input watermark after the official close of T "
            "— watermark violation, admission failure for that arm (B_idio "
            "class)",
            [l1_late, champion],
            {
                "ok": False,
                "reason_codes": ["watermark_after_close"],
                "failure_class": "idiosyncratic",
                "evidence_flags": [],
            },
            {
                "l1": {"reason_codes": ["watermark_after_close"],
                       "failure_class": "idiosyncratic",
                       "evidence_flags": []},
                "champion": arm_ok,
            },
        ),
        case(
            "divergent_retry",
            "champion has two records under ONE canonical job identity with "
            "different decision digests — integrity failure, never resolved "
            "by selecting the latest commit (B_idio class)",
            [l1, champion, champion_retry_divergent],
            {
                "ok": False,
                "reason_codes": ["divergent_retry"],
                "failure_class": "idiosyncratic",
                "evidence_flags": [],
            },
            {
                "l1": arm_ok,
                "champion": {"reason_codes": ["divergent_retry"],
                             "failure_class": "idiosyncratic",
                             "evidence_flags": []},
            },
        ),
        case(
            "missing_arm",
            "champion produced no record for the session — integrity "
            "failure (B_idio class)",
            [l1],
            {
                "ok": False,
                "reason_codes": ["missing_arm"],
                "failure_class": "idiosyncratic",
                "evidence_flags": [],
            },
            {"l1": arm_ok},
        ),
        case(
            "shared_price_source_failure",
            "both arms declare the same price-source failure — symmetric "
            "shared outage invalidates both arms (B_shared class)",
            [
                failure_record("l1", "price_source",
                               "primary price feed returned no prints"),
                failure_record("champion", "price_source",
                               "primary price feed returned no prints"),
            ],
            {
                "ok": False,
                "reason_codes": ["shared_price_source_failure"],
                "failure_class": "shared",
                "evidence_flags": [],
            },
            {
                "l1": {"reason_codes": ["arm_declared_failure"],
                       "failure_class": None, "evidence_flags": []},
                "champion": {"reason_codes": ["arm_declared_failure"],
                             "failure_class": None, "evidence_flags": []},
            },
        ),
        case(
            "asymmetric_valuation_failure",
            "champion alone declares a valuation failure — asymmetric, "
            "classified to the B_idio budget",
            [l1, failure_record("champion", "valuation",
                                "mark-to-market failed for one holding")],
            {
                "ok": False,
                "reason_codes": ["asymmetric_arm_failure"],
                "failure_class": "idiosyncratic",
                "evidence_flags": [],
            },
            {
                "l1": arm_ok,
                "champion": {"reason_codes": ["arm_declared_failure"],
                             "failure_class": None, "evidence_flags": []},
            },
        ),
        case(
            "timestamp_outside_window",
            "champion's run-bundle timestamp falls after open(T+1) — "
            "EVIDENCE flag only, the record still qualifies (v4 §2: "
            "timestamp is evidence, never the information-set proof)",
            [l1, champion_ts_outside],
            {
                "ok": True,
                "reason_codes": [],
                "failure_class": None,
                "evidence_flags": ["champion:run_bundle_timestamp_outside_window"],
            },
            {
                "l1": arm_ok,
                "champion": {"reason_codes": [], "failure_class": None,
                             "evidence_flags": [
                                 "run_bundle_timestamp_outside_window"]},
            },
        ),
    ]


def main() -> None:
    document = {
        "contract": "renquant_pipeline.decision_schedule.validate_session_records",
        "contract_version": 1,
        "design": "renquant-model#61 DESIGN_AMENDMENT_v4 §2 (implementation step 1, §5)",
        "session": SESSION,
        "session_window": SESSION_WINDOW,
        "calendar_id": CALENDAR_ID,
        "price_source_id": PRICE_SOURCE_ID,
        "cases": build_cases(),
    }
    OUT.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(document['cases'])} cases)")


if __name__ == "__main__":
    main()
