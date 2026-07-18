"""G4 re-registration step 1 — public decision-schedule validation API.

Design source: renquant-model#61 (DESIGN_AMENDMENT_v4, §2 canonical
next-open observation; §5 implementation order step 1). Surfaces under
test:

1. the contract fixture vectors (``tests/fixtures/decision_schedule/
   vectors.json`` — the file a later step promotes to renquant-common,
   mirroring the bundle_contract precedent) — all seven §6(b)-adversarial
   shapes plus the admissible byte-identical retry;
2. deterministic job identity over ``{arm, T, artifact digests, config
   digest}``, pinned byte-equal to the repo's real hashing utility
   (``renquant_artifacts.contracts.hash_jsonable``) so the stdlib-only
   module can never drift from the shared canonicalization;
3. retry byte-identity — divergent duplicates are an integrity failure
   NEVER resolved by selecting the latest commit;
4. the watermark recompute-verification hook (v4 §2 r2: declared ==
   recomputed over manifested inputs; the callable is injectable so the
   orchestrator wires digest-resolved recomputation in step 2);
5. import-lightness — stdlib ONLY, strictly lighter than
   bundle_contract (no renquant_common either);
6. rejection-path edges (fail-closed guards) and verdict falsy
   semantics.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from renquant_artifacts.contracts import hash_jsonable
from renquant_pipeline.decision_schedule import (
    DECISION_SCHEDULE_CONTRACT_VERSION,
    EVIDENCE_TIMESTAMP_OUTSIDE_WINDOW,
    EXPECTED_ARMS,
    FAILURE_CLASS_IDIOSYNCRATIC,
    FAILURE_CLASS_SHARED,
    JOB_IDENTITY_SCHEMA,
    REASON_DIVERGENT_RETRY,
    REASON_FIELD_INVALID,
    REASON_FROZEN_IDENTIFIER_MISMATCH,
    REASON_JOB_IDENTITY_MISMATCH,
    REASON_ORDERS_NOT_NEXT_OPEN,
    REASON_RECORD_SCHEMA_UNSUPPORTED,
    REASON_SESSION_MISMATCH,
    REASON_UNEXPECTED_ARM,
    REASON_WATERMARK_AFTER_CLOSE,
    REASON_WATERMARK_RECOMPUTE_MISMATCH,
    ScheduleVerdict,
    SessionVerdict,
    SessionWindow,
    job_identity,
    recompute_watermark_from_manifest,
    validate_arm_record,
    validate_session_records,
)

VECTORS_PATH = (
    Path(__file__).parent / "fixtures" / "decision_schedule" / "vectors.json"
)
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in VECTORS["cases"]}
CASE_NAMES = sorted(CASES)


def _window(case: dict) -> SessionWindow:
    return SessionWindow.from_iso(**case["session_window"])


def _validate_case(case: dict) -> SessionVerdict:
    return validate_session_records(
        case["records"],
        session_window=_window(case),
        expected_calendar_id=case["expected_calendar_id"],
        expected_price_source_id=case["expected_price_source_id"],
    )


def _clone(obj):
    return json.loads(json.dumps(obj))


# ---------------------------------------------------------------------------
# 1. Contract fixture vectors
# ---------------------------------------------------------------------------

def test_vectors_file_pins_this_contract_version() -> None:
    assert VECTORS["contract_version"] == DECISION_SCHEDULE_CONTRACT_VERSION
    assert (
        VECTORS["contract"]
        == "renquant_pipeline.decision_schedule.validate_session_records"
    )
    assert set(CASES) == {
        "valid_pair",
        "valid_retry_byte_identical",
        "late_watermark",
        "divergent_retry",
        "missing_arm",
        "shared_price_source_failure",
        "asymmetric_valuation_failure",
        "timestamp_outside_window",
    }


@pytest.mark.parametrize("name", CASE_NAMES)
def test_vector_verdicts(name: str) -> None:
    case = CASES[name]
    verdict = _validate_case(case)
    expected = case["expected"]
    assert verdict.ok is expected["ok"], verdict
    assert list(verdict.reason_codes) == expected["reason_codes"], verdict
    assert verdict.failure_class == expected["failure_class"], verdict
    assert list(verdict.evidence_flags) == expected["evidence_flags"], verdict
    assert verdict.contract_version == DECISION_SCHEDULE_CONTRACT_VERSION


@pytest.mark.parametrize("name", CASE_NAMES)
def test_vector_arm_attribution(name: str) -> None:
    """Per-arm verdicts pin blame attribution — the runner's budget
    accounting (v4 §2 r2: exactly one budget per failed session) needs
    the failing arm without re-deriving it."""
    case = CASES[name]
    verdict = _validate_case(case)
    arm_verdicts = dict(verdict.arm_verdicts)
    expected_arms = case["expected_arm_verdicts"]
    assert set(arm_verdicts) == set(expected_arms)
    for arm, expected in expected_arms.items():
        got = arm_verdicts[arm]
        assert list(got.reason_codes) == expected["reason_codes"], (arm, got)
        assert got.failure_class == expected["failure_class"], (arm, got)
        assert list(got.evidence_flags) == expected["evidence_flags"], (arm, got)


@pytest.mark.parametrize("name", CASE_NAMES)
def test_vector_job_ids_are_the_deterministic_identity(name: str) -> None:
    """Every qualifying vector record carries a REAL job identity —
    recomputable from its own {arm, T, artifact digests, config digest}
    via the public function (digests come from the repo's real hashing
    utilities, never hand-written strings)."""
    for record in CASES[name]["records"]:
        if record.get("failure") is not None:
            continue
        assert record["job_id"] == job_identity(
            arm=record["arm"],
            decision_session=record["decision_session"],
            artifact_digests=record["artifact_digests"],
            config_digest=record["config_digest"],
        )


# ---------------------------------------------------------------------------
# 2. Deterministic job identity
# ---------------------------------------------------------------------------

_IDENTITY_KWARGS = dict(
    arm="champion",
    decision_session="2026-07-16",
    artifact_digests={
        "scorer": "sha256:" + "a" * 64,
        "calibrator": "sha256:" + "b" * 64,
    },
    config_digest="sha256:" + "c" * 64,
)


def test_job_identity_is_deterministic_and_order_invariant() -> None:
    first = job_identity(**_IDENTITY_KWARGS)
    assert first == job_identity(**_IDENTITY_KWARGS)
    reordered = dict(_IDENTITY_KWARGS)
    reordered["artifact_digests"] = dict(
        reversed(list(_IDENTITY_KWARGS["artifact_digests"].items()))
    )
    assert first == job_identity(**reordered)
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64


@pytest.mark.parametrize(
    "override",
    [
        {"arm": "l1"},
        {"decision_session": "2026-07-17"},
        {"config_digest": "sha256:" + "d" * 64},
        {"artifact_digests": {"scorer": "sha256:" + "a" * 64}},
    ],
)
def test_job_identity_is_sensitive_to_every_component(override: dict) -> None:
    changed = {**_IDENTITY_KWARGS, **override}
    assert job_identity(**changed) != job_identity(**_IDENTITY_KWARGS)


def test_job_identity_matches_the_repo_hashing_utility() -> None:
    """The stdlib-only canonicalization is byte-equal to the repo's real
    ``hash_jsonable`` on the same payload — the pin that lets the module
    stay import-light without forking the shared hashing form."""
    payload = {
        "schema": JOB_IDENTITY_SCHEMA,
        "arm": _IDENTITY_KWARGS["arm"],
        "decision_session": _IDENTITY_KWARGS["decision_session"],
        "artifact_digests": dict(
            sorted(_IDENTITY_KWARGS["artifact_digests"].items())
        ),
        "config_digest": _IDENTITY_KWARGS["config_digest"],
    }
    assert job_identity(**_IDENTITY_KWARGS) == hash_jsonable(payload)


# ---------------------------------------------------------------------------
# 3. Retry byte-identity
# ---------------------------------------------------------------------------

def _valid_case_pair() -> tuple[list[dict], SessionWindow, dict]:
    case = _clone(CASES["valid_pair"])
    return case["records"], _window(case), case


def test_byte_identical_retry_is_admissible_regardless_of_order() -> None:
    records, window, case = _valid_case_pair()
    retry = _clone(records[1])
    retry["run_bundle_timestamp"] = "2026-07-16T23:59:00+00:00"
    for ordering in ([*records, retry], [retry, *records]):
        verdict = validate_session_records(ordering, session_window=window)
        assert verdict.ok, verdict


@pytest.mark.parametrize("tamper", ["decision_digest", "input_digest"])
def test_divergent_duplicate_is_never_resolved_by_latest_commit(
    tamper: str,
) -> None:
    """A LATER record with a divergent decision or input digest does not
    win — the arm fails with ``divergent_retry`` in either ordering."""
    records, window, _ = _valid_case_pair()
    divergent = _clone(records[1])
    divergent["run_bundle_timestamp"] = "2026-07-16T23:59:00+00:00"
    if tamper == "decision_digest":
        divergent["decision_digest"] = "sha256:" + "f" * 64
    else:
        divergent["input_manifest"]["prices_daily"]["digest"] = (
            "sha256:" + "f" * 64
        )
    for ordering in ([*records, divergent], [divergent, *records]):
        verdict = validate_session_records(ordering, session_window=window)
        assert not verdict.ok
        assert REASON_DIVERGENT_RETRY in verdict.reason_codes
        assert verdict.failure_class == FAILURE_CLASS_IDIOSYNCRATIC
        arm_verdicts = dict(verdict.arm_verdicts)
        assert REASON_DIVERGENT_RETRY in arm_verdicts["champion"].reason_codes
        assert arm_verdicts["l1"].ok


# ---------------------------------------------------------------------------
# 4. Watermark recompute-verification hook (v4 §2 r2)
# ---------------------------------------------------------------------------

def test_recompute_hook_receives_the_manifested_inputs() -> None:
    """The injectable callable gets the record's input manifest (entries
    carry the input DIGESTS, so the orchestrator's step-2 hook can
    resolve bytes by digest and recompute event times for real)."""
    records, window, _ = _valid_case_pair()
    seen: list[dict] = []

    def hook(manifest):
        seen.append(dict(manifest))
        return recompute_watermark_from_manifest(manifest)

    verdict = validate_session_records(
        records, session_window=window, recompute_max_event_time=hook
    )
    assert verdict.ok, verdict
    assert len(seen) == len(records)
    for manifest in seen:
        for entry in manifest.values():
            assert entry["digest"].startswith("sha256:")


def test_recompute_hook_mismatch_is_an_admission_failure() -> None:
    """declared != recomputed => admission failure for that arm, B_idio
    class (v4 §2 r2: the declared watermark is not self-certifying)."""
    records, window, _ = _valid_case_pair()
    verdict = validate_session_records(
        records,
        session_window=window,
        recompute_max_event_time=lambda m: "2026-07-16T19:00:00+00:00",
    )
    assert not verdict.ok
    assert verdict.reason_codes == (REASON_WATERMARK_RECOMPUTE_MISMATCH,)
    assert verdict.failure_class == FAILURE_CLASS_IDIOSYNCRATIC


def test_recompute_hook_failure_is_fail_closed() -> None:
    """A hook that cannot recompute (returns None) never admits the
    declared value on trust."""
    records, window, _ = _valid_case_pair()
    verdict = validate_session_records(
        records, session_window=window, recompute_max_event_time=lambda m: None
    )
    assert not verdict.ok
    assert verdict.reason_codes == (REASON_WATERMARK_RECOMPUTE_MISMATCH,)


def test_default_recompute_checks_the_manifest_event_times() -> None:
    """Without an injected hook, a declared watermark inconsistent with
    the manifest's own per-input max event-times is refused."""
    records, window, _ = _valid_case_pair()
    tampered = _clone(records)
    tampered[0]["declared_input_watermark"] = "2026-07-16T19:00:00+00:00"
    verdict = validate_session_records(tampered, session_window=window)
    assert not verdict.ok
    assert verdict.reason_codes == (REASON_WATERMARK_RECOMPUTE_MISMATCH,)


def test_watermark_equality_is_instant_based_not_textual() -> None:
    """The same instant in a different UTC offset is the same watermark."""
    records, window, _ = _valid_case_pair()
    shifted = _clone(records)
    declared = dt.datetime.fromisoformat(
        shifted[0]["declared_input_watermark"]
    ).astimezone(dt.timezone(dt.timedelta(hours=2)))
    shifted[0]["declared_input_watermark"] = declared.isoformat()
    verdict = validate_session_records(shifted, session_window=window)
    assert verdict.ok, verdict


def test_reference_recompute_helper_max_and_fail_closed() -> None:
    manifest = {
        "a": {"digest": "sha256:" + "0" * 64,
              "max_event_time": "2026-07-16T11:00:00+00:00"},
        "b": {"digest": "sha256:" + "1" * 64,
              "max_event_time": "2026-07-16T19:59:57+00:00"},
    }
    recomputed = recompute_watermark_from_manifest(manifest)
    assert recomputed == dt.datetime.fromisoformat("2026-07-16T19:59:57+00:00")
    assert recompute_watermark_from_manifest({}) is None
    assert recompute_watermark_from_manifest(
        {"a": {"digest": "sha256:" + "0" * 64}}
    ) is None
    assert recompute_watermark_from_manifest(
        {"a": {"max_event_time": "2026-07-16T12:00:00"}}  # naive => refused
    ) is None


# ---------------------------------------------------------------------------
# 5. Import-lightness
# ---------------------------------------------------------------------------

#: Strictly lighter than bundle_contract: stdlib ONLY. Any renquant
#: sibling package or scientific/runtime dep is forbidden at import.
_FORBIDDEN_ROOTS = (
    "pandas", "numpy", "scipy", "xgboost", "torch", "transformers",
    "lightgbm", "catboost", "cvxpy",
    "renquant_common", "renquant_artifacts", "renquant_base_data",
)
_FORBIDDEN_MODULES = (
    "renquant_pipeline.inference",
    "renquant_pipeline.panel_scoring",
    "renquant_pipeline.bundle_contract",
    "renquant_pipeline.kernel.pipeline",
    "renquant_pipeline.kernel.panel_pipeline.job_panel_scoring",
    "renquant_pipeline.kernel.panel_pipeline.panel_scorer",
)


def test_decision_schedule_import_is_light() -> None:
    code = (
        "import json, sys\n"
        "import renquant_pipeline.decision_schedule\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, check=True,
    )
    modules = set(json.loads(proc.stdout))
    offenders = sorted(
        m for m in modules
        if m.split(".", 1)[0] in _FORBIDDEN_ROOTS or m in _FORBIDDEN_MODULES
    )
    assert offenders == [], (
        "renquant_pipeline.decision_schedule must stay stdlib-only — the "
        "model repo's backfill and the orchestrator's admission ledger "
        "import it as a pure contract (v4 §5); offending imports: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# 6. Fail-closed guards and verdict semantics
# ---------------------------------------------------------------------------

def test_unknown_record_schema_fails_closed() -> None:
    records, window, _ = _valid_case_pair()
    bad = _clone(records)
    bad[0]["schema_version"] = 2
    verdict = validate_session_records(bad, session_window=window)
    assert not verdict.ok
    assert REASON_RECORD_SCHEMA_UNSUPPORTED in verdict.reason_codes


def test_naive_watermark_timestamp_is_refused() -> None:
    records, window, _ = _valid_case_pair()
    bad = _clone(records)
    bad[0]["declared_input_watermark"] = "2026-07-16T19:59:57"  # naive
    verdict = validate_session_records(bad, session_window=window)
    assert not verdict.ok
    assert REASON_FIELD_INVALID in verdict.reason_codes


def test_orders_for_the_wrong_session_are_refused() -> None:
    records, window, _ = _valid_case_pair()
    bad = _clone(records)
    bad[0]["orders_scheduled_for"] = "2026-07-20"
    verdict = validate_session_records(bad, session_window=window)
    assert not verdict.ok
    assert REASON_ORDERS_NOT_NEXT_OPEN in verdict.reason_codes


def test_empty_declared_order_set_is_a_valid_no_trade() -> None:
    records, window, _ = _valid_case_pair()
    no_trade = _clone(records)
    no_trade[0]["orders"] = []
    verdict = validate_session_records(no_trade, session_window=window)
    assert verdict.ok, verdict


def test_tampered_job_id_is_refused() -> None:
    records, window, _ = _valid_case_pair()
    bad = _clone(records)
    bad[0]["job_id"] = "sha256:" + "9" * 64
    verdict = validate_session_records(bad, session_window=window)
    assert not verdict.ok
    assert REASON_JOB_IDENTITY_MISMATCH in verdict.reason_codes


def test_unexpected_arm_is_refused() -> None:
    records, window, _ = _valid_case_pair()
    stray = _clone(records[0])
    stray["arm"] = "l2"
    verdict = validate_session_records(
        [*records, stray], session_window=window
    )
    assert not verdict.ok
    assert REASON_UNEXPECTED_ARM in verdict.reason_codes


def test_mixed_sessions_are_refused() -> None:
    records, window, _ = _valid_case_pair()
    other = _clone(records[1])
    other["decision_session"] = "2026-07-17"
    verdict = validate_session_records(
        [records[0], other], session_window=window
    )
    assert not verdict.ok
    assert REASON_SESSION_MISMATCH in verdict.reason_codes


def test_frozen_identifier_mismatch_is_refused() -> None:
    records, window, case = _valid_case_pair()
    verdict = validate_session_records(
        records,
        session_window=window,
        expected_calendar_id="nyse/2099.01",
        expected_price_source_id=case["expected_price_source_id"],
    )
    assert not verdict.ok
    assert REASON_FROZEN_IDENTIFIER_MISMATCH in verdict.reason_codes


def test_watermark_exactly_at_close_is_admissible() -> None:
    """v4 §2: 'no later than the official close' — the boundary instant
    itself qualifies."""
    records, window, _ = _valid_case_pair()
    at_close = _clone(records)
    close_iso = window.close.isoformat()
    at_close[0]["declared_input_watermark"] = close_iso
    at_close[0]["input_manifest"]["prices_daily"]["max_event_time"] = close_iso
    verdict = validate_session_records(at_close, session_window=window)
    assert verdict.ok, verdict


def test_arm_record_late_watermark_direct() -> None:
    records, window, _ = _valid_case_pair()
    late = _clone(records[0])
    after_close = "2026-07-16T20:00:01+00:00"
    late["declared_input_watermark"] = after_close
    late["input_manifest"]["prices_daily"]["max_event_time"] = after_close
    verdict = validate_arm_record(late, session_window=window)
    assert verdict.reason_codes == (REASON_WATERMARK_AFTER_CLOSE,)
    assert verdict.failure_class == FAILURE_CLASS_IDIOSYNCRATIC


def test_timestamp_outside_window_is_evidence_not_failure_direct() -> None:
    records, window, _ = _valid_case_pair()
    outside = _clone(records[0])
    outside["run_bundle_timestamp"] = "2026-07-16T19:00:00+00:00"  # pre-close
    verdict = validate_arm_record(outside, session_window=window)
    assert verdict.ok
    assert verdict.evidence_flags == (EVIDENCE_TIMESTAMP_OUTSIDE_WINDOW,)


def test_session_window_construction_is_strict() -> None:
    with pytest.raises(ValueError):
        SessionWindow(
            close=dt.datetime(2026, 7, 16, 20),  # naive
            next_open=dt.datetime(2026, 7, 17, 13, 30, tzinfo=dt.timezone.utc),
            next_open_session="2026-07-17",
        )
    with pytest.raises(ValueError):
        SessionWindow.from_iso(
            close="2026-07-17T13:30:00+00:00",
            next_open="2026-07-16T20:00:00+00:00",  # misordered
            next_open_session="2026-07-17",
        )


def test_verdict_falsy_semantics_and_classes() -> None:
    accept = ScheduleVerdict(ok=True)
    reject = ScheduleVerdict(
        ok=False,
        reason_codes=(REASON_WATERMARK_AFTER_CLOSE,),
        failure_class=FAILURE_CLASS_IDIOSYNCRATIC,
    )
    assert bool(accept) and not bool(reject)
    session_accept = SessionVerdict(ok=True)
    session_reject = SessionVerdict(
        ok=False, reason_codes=("missing_arm",),
        failure_class=FAILURE_CLASS_IDIOSYNCRATIC,
    )
    assert bool(session_accept) and not bool(session_reject)
    assert FAILURE_CLASS_SHARED != FAILURE_CLASS_IDIOSYNCRATIC
    assert EXPECTED_ARMS == ("l1", "champion")
