"""Public decision-schedule record-validation API — G4 re-registration step 1.

Implements the §2 "one canonical next-open observation" contract of the
merged v4 amendment (renquant-model#61,
``experiments/ensemble_phase0/DESIGN_AMENDMENT_v4_executable_next_open_
evaluation.md``): for decision session T, a qualifying record for each
arm must have an immutable ``decision_session=T``, a declared input
watermark no later than the official close of T, a complete
input/artifact manifest, a run-bundle timestamp in
``(close(T), open(T+1))`` (EVIDENCE only, never the information-set
proof), frozen calendar and price-source identifiers, a declared order
set scheduled for the first regular-session open of T+1, and a
deterministic job identity over ``{arm, T, artifact digests, config
digest}``. Retried jobs must have byte-identical decision and input
digests; divergent duplicates, a watermark violation, or a missing arm
make the session an integrity failure and are NEVER resolved by
selecting the latest commit.

Ownership (v4 §5): renquant-pipeline owns this public, versioned
schedule/record-validation API; renquant-orchestrator owns the daily
job, fill/price collection, admission-ledger execution, and run bundle
(implementation step 2); renquant-model's score backfill and PIT ledger
CONSUME this contract (step 3). No private cross-repo as-of helper
survives anywhere (v4 supersession of v3 §E1).

Scope notes (contract v1):

* Validation + failure CLASSIFICATION only. The verdict labels an
  inadmissible session ``idiosyncratic`` (asymmetric — v4 §2 r2 counts
  it against ``B_idio``) or ``shared`` (symmetric/shared — ``B_shared``).
  Budget ACCOUNTING — the frozen budget sizes, cumulative counts, and
  the terminal ``NO-GO (integrity)`` consequence — is the runner's job
  (orchestrator, step 2), never decided here.
* The declared input watermark is NOT self-certifying (v4 §2 r2): this
  API recomputes the maximum event-time over the manifested inputs and
  admission REQUIRES declared == recomputed; any mismatch is an
  admission failure for that arm. The default recomputation reads the
  per-input ``max_event_time`` fields of the record's own manifest;
  production admission MUST inject ``recompute_max_event_time`` — a
  callable that re-reads the manifested inputs BY DIGEST and recomputes
  event times from bytes (orchestrator wiring, step 2).
* The run-bundle timestamp window check is EVIDENCE only: an
  out-of-window timestamp raises an evidence flag on an otherwise
  qualifying record, never an admission failure (v4 §2: "timestamp is
  evidence only, never the information-set proof").
* Resolving the frozen, versioned calendar (``calendar_id`` → the
  official close of T and the first regular-session open of T+1) is the
  caller's job via ``renquant_common.market_calendar``; this module
  validates records against the resolved :class:`SessionWindow` and
  stays stdlib-only (import-lightness is pinned by
  ``tests/test_decision_schedule.py`` in a subprocess, mirroring
  ``bundle_contract``).

Import-lightness contract: this module (and the lazy package
``__init__`` it forces) must stay importable with the standard library
ONLY — no pandas/numpy/xgboost/torch/cvxpy, no renquant_common, no
renquant_artifacts (strictly lighter than ``bundle_contract``; the
verdicts must be constructible from any consumer, including the model
repo's backfill, without the runtime stack).

Classification rules pinned by contract v1 where v4 is silent (each is
reported in the PR that introduced it, not improvised later):

* A symmetric declared failure (both arms, same kind, kind in
  :data:`SHARED_FAILURE_KINDS`) classifies the session ``shared`` even
  when other admission codes coexist — under a documented shared outage
  the arms cannot produce clean records, so residual admission noise is
  downstream of the outage. All reason codes are still reported.
* Any other inadmissible outcome — missing arm, divergent retry,
  watermark violation/recompute mismatch, asymmetric declared failure,
  both arms failing with DIFFERENT kinds — classifies ``idiosyncratic``.
* A symmetric admission failure (e.g. both arms late) is NOT a
  documented shared outage: ``idiosyncratic``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: Version of THIS validation contract (bump on any semantic change; the
#: fixture vectors in ``tests/fixtures/decision_schedule/`` pin verdict
#: semantics per version and are the file a later step promotes to
#: renquant-common, mirroring the bundle_contract precedent).
DECISION_SCHEDULE_CONTRACT_VERSION = 1

#: Schema version a v1 record must declare (``schema_version`` field).
RECORD_SCHEMA_VERSION = 1

#: Frozen arm labels of the registered pair (v4 §3: L1 is the single
#: frozen equal-weight combination; the champion is a single frozen
#: artifact/configuration digest).
ARM_L1 = "l1"
ARM_CHAMPION = "champion"
EXPECTED_ARMS: tuple[str, ...] = (ARM_L1, ARM_CHAMPION)

#: Failure classes — classification OUTPUT only (v4 §2 r2 budget rule:
#: asymmetric → ``B_idio``; symmetric/shared → ``B_shared``). Budget
#: accounting is the runner's job.
FAILURE_CLASS_IDIOSYNCRATIC = "idiosyncratic"
FAILURE_CLASS_SHARED = "shared"

#: Declared failure kinds (record ``failure.kind``). ``B_idio`` names
#: job crash, missing arm, divergent retry, asymmetric
#: valuation/fill/price failure; ``B_shared`` names documented shared
#: venue/calendar outages including the §3 common price-source failure.
FAILURE_KIND_JOB_CRASH = "job_crash"
FAILURE_KIND_VALUATION = "valuation"
FAILURE_KIND_FILL = "fill"
FAILURE_KIND_PRICE_SOURCE = "price_source"
FAILURE_KIND_VENUE_OUTAGE = "venue_outage"
FAILURE_KIND_CALENDAR_OUTAGE = "calendar_outage"
KNOWN_FAILURE_KINDS = frozenset(
    {
        FAILURE_KIND_JOB_CRASH,
        FAILURE_KIND_VALUATION,
        FAILURE_KIND_FILL,
        FAILURE_KIND_PRICE_SOURCE,
        FAILURE_KIND_VENUE_OUTAGE,
        FAILURE_KIND_CALENDAR_OUTAGE,
    }
)
#: Kinds that classify ``shared`` when BOTH arms declare the SAME kind.
SHARED_FAILURE_KINDS = frozenset(
    {
        FAILURE_KIND_PRICE_SOURCE,
        FAILURE_KIND_VENUE_OUTAGE,
        FAILURE_KIND_CALENDAR_OUTAGE,
    }
)

# ---------------------------------------------------------------------------
# Reason codes — stable strings, part of the versioned contract.
# ---------------------------------------------------------------------------
# Record-level (per-arm admission):
REASON_RECORD_SCHEMA_UNSUPPORTED = "record_schema_unsupported"
REASON_FIELD_MISSING = "field_missing"
REASON_FIELD_INVALID = "field_invalid"
REASON_FROZEN_IDENTIFIER_MISMATCH = "frozen_identifier_mismatch"
REASON_JOB_IDENTITY_MISMATCH = "job_identity_mismatch"
REASON_WATERMARK_AFTER_CLOSE = "watermark_after_close"
REASON_WATERMARK_RECOMPUTE_MISMATCH = "watermark_recompute_mismatch"
REASON_ORDERS_NOT_NEXT_OPEN = "orders_not_for_next_open"
REASON_ARM_DECLARED_FAILURE = "arm_declared_failure"
# Session-level (pairing/integrity):
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_MISSING_ARM = "missing_arm"
REASON_UNEXPECTED_ARM = "unexpected_arm"
REASON_DIVERGENT_RETRY = "divergent_retry"
REASON_ASYMMETRIC_ARM_FAILURE = "asymmetric_arm_failure"
REASON_SHARED_PRICE_SOURCE_FAILURE = "shared_price_source_failure"
REASON_SHARED_OUTAGE_FAILURE = "shared_outage_failure"

#: Evidence flags — never failures (v4 §2: timestamp is evidence only).
EVIDENCE_TIMESTAMP_OUTSIDE_WINDOW = "run_bundle_timestamp_outside_window"
EVIDENCE_FAILED_ATTEMPT_RECORDED = "failed_attempt_recorded"

#: Canonical schema tag hashed into every job identity.
JOB_IDENTITY_SCHEMA = "decision_schedule/job_identity/v1"

_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LEN = 64

#: Fields a QUALIFYING (non-failure) v1 record must carry.
QUALIFYING_RECORD_FIELDS: tuple[str, ...] = (
    "schema_version",
    "arm",
    "decision_session",
    "declared_input_watermark",
    "input_manifest",
    "artifact_digests",
    "config_digest",
    "job_id",
    "run_bundle_timestamp",
    "calendar_id",
    "price_source_id",
    "orders",
    "orders_scheduled_for",
    "decision_digest",
)


@dataclass(frozen=True)
class SessionWindow:
    """The frozen calendar's resolution of decision session T.

    ``close`` is the official close of T; ``next_open`` is the first
    regular-session open after T; ``next_open_session`` is that
    session's date (ISO ``YYYY-MM-DD``). Both datetimes must be
    timezone-aware and ordered — resolving them from the record's
    ``calendar_id`` via ``renquant_common.market_calendar`` is the
    caller's job (orchestrator, step 2). Construction errors raise
    (caller input, not a validation outcome).
    """

    close: dt.datetime
    next_open: dt.datetime
    next_open_session: str

    def __post_init__(self) -> None:
        for name, value in (("close", self.close), ("next_open", self.next_open)):
            if not isinstance(value, dt.datetime) or value.tzinfo is None:
                raise ValueError(
                    f"SessionWindow.{name} must be a timezone-aware datetime, "
                    f"got {value!r}"
                )
        if self.close >= self.next_open:
            raise ValueError(
                f"SessionWindow close {self.close.isoformat()} must precede "
                f"next_open {self.next_open.isoformat()}"
            )
        dt.date.fromisoformat(self.next_open_session)  # raises on bad input

    @classmethod
    def from_iso(
        cls, *, close: str, next_open: str, next_open_session: str
    ) -> "SessionWindow":
        return cls(
            close=dt.datetime.fromisoformat(close),
            next_open=dt.datetime.fromisoformat(next_open),
            next_open_session=next_open_session,
        )


@dataclass(frozen=True)
class ScheduleVerdict:
    """Structured per-arm verdict.

    ``ok`` is the load-bearing field and ``bool(verdict)`` mirrors it
    (same seam rule as ``bundle_contract.PairVerdict``). ``failure_class``
    is classification OUTPUT only: ``"idiosyncratic"`` for per-arm
    admission failures (v4 §2 r2 counts them against ``B_idio`` for that
    arm), ``None`` when ok — and ``None`` on a declared-failure record,
    whose class is decided at session level from arm symmetry.
    ``evidence_flags`` never affect ``ok``.
    """

    ok: bool
    reason_codes: tuple[str, ...] = ()
    failure_class: "str | None" = None
    evidence_flags: tuple[str, ...] = ()
    detail: str = ""
    contract_version: int = field(default=DECISION_SCHEDULE_CONTRACT_VERSION)

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class SessionVerdict:
    """Structured verdict for one decision session (all arms).

    ``arm_verdicts`` carries per-arm attribution — ``(arm, verdict)``
    pairs in :data:`EXPECTED_ARMS` order, each verdict merged across
    that arm's records — so the runner can count an inadmissible session
    against exactly one budget without re-deriving blame. ``ok`` is
    load-bearing; ``bool(verdict)`` mirrors it.
    """

    ok: bool
    reason_codes: tuple[str, ...] = ()
    failure_class: "str | None" = None
    evidence_flags: tuple[str, ...] = ()
    arm_verdicts: tuple[tuple[str, ScheduleVerdict], ...] = ()
    detail: str = ""
    contract_version: int = field(default=DECISION_SCHEDULE_CONTRACT_VERSION)

    def __bool__(self) -> bool:
        return self.ok


def job_identity(
    *,
    arm: str,
    decision_session: str,
    artifact_digests: Mapping[str, str],
    config_digest: str,
) -> str:
    """Deterministic job identity over ``{arm, T, artifact digests,
    config digest}`` (v4 §2: exactly one canonical job identity per
    arm/session).

    Canonicalization deliberately matches the repo's real hashing
    utility ``renquant_artifacts.contracts.hash_jsonable`` (sorted keys,
    compact separators, ``sha256:`` prefix) WITHOUT importing it — this
    module must stay stdlib-only; ``tests/test_decision_schedule.py``
    pins byte-equality against the real ``hash_jsonable`` so the two can
    never drift silently.
    """
    payload = {
        "schema": JOB_IDENTITY_SCHEMA,
        "arm": str(arm),
        "decision_session": str(decision_session),
        "artifact_digests": {
            str(k): str(v) for k, v in sorted(artifact_digests.items())
        },
        "config_digest": str(config_digest),
    }
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _DIGEST_PREFIX + hashlib.sha256(blob).hexdigest()


def recompute_watermark_from_manifest(
    input_manifest: Mapping[str, Mapping[str, Any]],
) -> "dt.datetime | None":
    """Reference recomputation: max event-time across manifest entries.

    This is the DEFAULT ``recompute_max_event_time`` hook — it
    recomputes the watermark from the per-input ``max_event_time``
    fields the record's own manifest declares, catching a top-level
    declaration inconsistent with its manifest. Production admission
    MUST inject a callable that resolves the manifested inputs BY DIGEST
    and recomputes event times from bytes (v4 §2 r2: the declared
    watermark is not self-certifying) — that wiring is orchestrator
    step 2. Returns ``None`` when recomputation is impossible (empty
    manifest, missing/unparseable entry times); the validator treats
    ``None`` as a recompute mismatch, fail-closed.
    """
    times: list[dt.datetime] = []
    if not isinstance(input_manifest, Mapping) or not input_manifest:
        return None
    for entry in input_manifest.values():
        if not isinstance(entry, Mapping):
            return None
        ts = _parse_aware(entry.get("max_event_time"))
        if ts is None:
            return None
        times.append(ts)
    return max(times)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_aware(value: Any) -> "dt.datetime | None":
    """Parse an ISO-8601 timestamp; timezone-aware required (fail-closed:
    a naive timestamp cannot be compared against the frozen calendar)."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(_DIGEST_PREFIX)
        and len(value) == len(_DIGEST_PREFIX) + _DIGEST_HEX_LEN
        and all(c in "0123456789abcdef" for c in value[len(_DIGEST_PREFIX):])
    )


def _is_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


class _Collector:
    """Ordered, deduplicating accumulator for reason codes and details."""

    def __init__(self) -> None:
        self.codes: list[str] = []
        self.details: list[str] = []

    def add(self, code: str, detail: str) -> None:
        if code not in self.codes:
            self.codes.append(code)
        self.details.append(detail)


def _declared_failure(record: Mapping[str, Any]) -> "Mapping[str, Any] | None":
    failure = record.get("failure")
    return failure if isinstance(failure, Mapping) else None


def _validate_declared_failure_record(
    record: Mapping[str, Any], failure: Mapping[str, Any]
) -> ScheduleVerdict:
    """A declared-failure record is NOT a qualifying record: it needs only
    the identifying fields plus a known failure kind, and its class is
    decided at session level from arm symmetry (v4 §2 r2)."""
    collector = _Collector()
    kind = failure.get("kind")
    if kind not in KNOWN_FAILURE_KINDS:
        collector.add(
            REASON_FIELD_INVALID,
            f"failure.kind {kind!r} is not one of {sorted(KNOWN_FAILURE_KINDS)}",
        )
    for name in ("arm", "decision_session"):
        if not record.get(name):
            collector.add(REASON_FIELD_MISSING, f"failure record missing {name!r}")
    collector.add(
        REASON_ARM_DECLARED_FAILURE,
        f"arm {record.get('arm')!r} declared failure kind={kind!r}: "
        f"{failure.get('detail', '')}",
    )
    return ScheduleVerdict(
        ok=False,
        reason_codes=tuple(collector.codes),
        failure_class=None,
        detail="; ".join(collector.details),
    )


def validate_arm_record(
    record: Mapping[str, Any],
    *,
    session_window: SessionWindow,
    recompute_max_event_time: "Callable[[Mapping[str, Mapping[str, Any]]], Any] | None" = None,
    expected_calendar_id: "str | None" = None,
    expected_price_source_id: "str | None" = None,
) -> ScheduleVerdict:
    """Validate ONE arm's next-open record against v4 §2.

    Never raises on a validation outcome — every rejection is a
    structured :class:`ScheduleVerdict` with stable reason codes, and
    independent check failures are COLLECTED (fixed order, deduplicated)
    rather than first-failure-only, so an admission ledger records the
    full forensic picture in one pass.

    ``recompute_max_event_time`` is the v4 §2 r2 watermark-verification
    hook: it receives the record's ``input_manifest`` (entries carry the
    input digests, so a real implementation resolves bytes by digest)
    and must return the recomputed maximum event-time; admission
    REQUIRES declared == recomputed. When ``None``, the reference
    :func:`recompute_watermark_from_manifest` is used — see its
    docstring for why production admission must inject the real hook.
    """
    if not isinstance(record, Mapping):
        return ScheduleVerdict(
            ok=False,
            reason_codes=(REASON_FIELD_INVALID,),
            failure_class=FAILURE_CLASS_IDIOSYNCRATIC,
            detail=f"record must be a mapping, got {type(record).__name__}",
        )

    schema_version = record.get("schema_version")
    if schema_version != RECORD_SCHEMA_VERSION:
        return ScheduleVerdict(
            ok=False,
            reason_codes=(REASON_RECORD_SCHEMA_UNSUPPORTED,),
            failure_class=FAILURE_CLASS_IDIOSYNCRATIC,
            detail=(
                f"record schema_version {schema_version!r} is not supported by "
                f"decision-schedule contract v{DECISION_SCHEDULE_CONTRACT_VERSION} "
                "(fail-closed on unknown schemas)"
            ),
        )

    failure = _declared_failure(record)
    if failure is not None:
        return _validate_declared_failure_record(record, failure)

    collector = _Collector()
    evidence: list[str] = []

    for name in QUALIFYING_RECORD_FIELDS:
        if name not in record:
            collector.add(REASON_FIELD_MISSING, f"missing required field {name!r}")

    arm = record.get("arm")
    if "arm" in record and not (isinstance(arm, str) and arm):
        collector.add(REASON_FIELD_INVALID, f"arm must be a non-empty string, got {arm!r}")

    session = record.get("decision_session")
    if "decision_session" in record and not _is_date(session):
        collector.add(
            REASON_FIELD_INVALID,
            f"decision_session must be an ISO date, got {session!r}",
        )

    for name, expected in (
        ("calendar_id", expected_calendar_id),
        ("price_source_id", expected_price_source_id),
    ):
        value = record.get(name)
        if name in record and not (isinstance(value, str) and value):
            collector.add(
                REASON_FIELD_INVALID, f"{name} must be a non-empty string, got {value!r}"
            )
        elif name in record and expected is not None and value != expected:
            collector.add(
                REASON_FROZEN_IDENTIFIER_MISMATCH,
                f"{name} {value!r} does not match the frozen registration "
                f"value {expected!r}",
            )

    manifest = record.get("input_manifest")
    manifest_ok = isinstance(manifest, Mapping) and bool(manifest)
    if "input_manifest" in record and not manifest_ok:
        collector.add(
            REASON_FIELD_INVALID,
            "input_manifest must be a non-empty mapping of input name -> "
            f"{{digest, max_event_time}}, got {manifest!r}",
        )
    elif manifest_ok:
        for name, entry in manifest.items():
            if not isinstance(entry, Mapping) or not _is_digest(entry.get("digest")):
                collector.add(
                    REASON_FIELD_INVALID,
                    f"input_manifest[{name!r}] must carry a sha256 digest",
                )
                manifest_ok = False

    artifact_digests = record.get("artifact_digests")
    digests_ok = isinstance(artifact_digests, Mapping) and bool(artifact_digests)
    if "artifact_digests" in record and not digests_ok:
        collector.add(
            REASON_FIELD_INVALID,
            f"artifact_digests must be a non-empty mapping, got {artifact_digests!r}",
        )
    elif digests_ok:
        for name, value in artifact_digests.items():
            if not _is_digest(value):
                collector.add(
                    REASON_FIELD_INVALID,
                    f"artifact_digests[{name!r}] is not a sha256 digest: {value!r}",
                )
                digests_ok = False

    config_digest = record.get("config_digest")
    config_ok = _is_digest(config_digest)
    if "config_digest" in record and not config_ok:
        collector.add(
            REASON_FIELD_INVALID,
            f"config_digest is not a sha256 digest: {config_digest!r}",
        )

    decision_digest = record.get("decision_digest")
    if "decision_digest" in record and not _is_digest(decision_digest):
        collector.add(
            REASON_FIELD_INVALID,
            f"decision_digest is not a sha256 digest: {decision_digest!r}",
        )

    job_id = record.get("job_id")
    if "job_id" in record and not _is_digest(job_id):
        collector.add(
            REASON_FIELD_INVALID, f"job_id is not a sha256 digest: {job_id!r}"
        )
    elif (
        "job_id" in record
        and isinstance(arm, str)
        and arm
        and _is_date(session)
        and digests_ok
        and config_ok
    ):
        recomputed_id = job_identity(
            arm=arm,
            decision_session=session,
            artifact_digests=artifact_digests,
            config_digest=config_digest,
        )
        if job_id != recomputed_id:
            collector.add(
                REASON_JOB_IDENTITY_MISMATCH,
                f"declared job_id {job_id} != deterministic identity "
                f"{recomputed_id} over {{arm, T, artifact digests, config digest}}",
            )

    declared_watermark = _parse_aware(record.get("declared_input_watermark"))
    if "declared_input_watermark" in record and declared_watermark is None:
        collector.add(
            REASON_FIELD_INVALID,
            "declared_input_watermark must be a timezone-aware ISO-8601 "
            f"timestamp, got {record.get('declared_input_watermark')!r}",
        )
    elif declared_watermark is not None:
        if declared_watermark > session_window.close:
            collector.add(
                REASON_WATERMARK_AFTER_CLOSE,
                f"declared input watermark {declared_watermark.isoformat()} is "
                "after the official close "
                f"{session_window.close.isoformat()} of session {session!r}",
            )
        if manifest_ok:
            hook = recompute_max_event_time or recompute_watermark_from_manifest
            recomputed = _parse_aware(hook(manifest))
            if recomputed is None or recomputed != declared_watermark:
                collector.add(
                    REASON_WATERMARK_RECOMPUTE_MISMATCH,
                    "declared input watermark "
                    f"{declared_watermark.isoformat()} != recomputed max "
                    "event-time over manifested inputs "
                    f"({recomputed.isoformat() if recomputed else 'unrecomputable'})"
                    " — the declared watermark is not self-certifying (v4 §2 r2)",
                )

    orders = record.get("orders")
    if "orders" in record and not (
        isinstance(orders, Sequence)
        and not isinstance(orders, (str, bytes))
        and all(isinstance(o, Mapping) for o in orders)
    ):
        collector.add(
            REASON_FIELD_INVALID,
            "orders must be a list of order mappings (empty list = declared "
            f"no-trade), got {orders!r}",
        )

    scheduled_for = record.get("orders_scheduled_for")
    if "orders_scheduled_for" in record and not _is_date(scheduled_for):
        collector.add(
            REASON_FIELD_INVALID,
            f"orders_scheduled_for must be an ISO date, got {scheduled_for!r}",
        )
    elif (
        _is_date(scheduled_for)
        and scheduled_for != session_window.next_open_session
    ):
        collector.add(
            REASON_ORDERS_NOT_NEXT_OPEN,
            f"declared order set is scheduled for {scheduled_for!r}, not the "
            "first regular-session open after T "
            f"({session_window.next_open_session!r})",
        )

    bundle_ts = _parse_aware(record.get("run_bundle_timestamp"))
    if "run_bundle_timestamp" in record and bundle_ts is None:
        collector.add(
            REASON_FIELD_INVALID,
            "run_bundle_timestamp must be a timezone-aware ISO-8601 "
            f"timestamp, got {record.get('run_bundle_timestamp')!r}",
        )
    elif bundle_ts is not None and not (
        session_window.close < bundle_ts < session_window.next_open
    ):
        # EVIDENCE only — never an admission failure (v4 §2).
        evidence.append(EVIDENCE_TIMESTAMP_OUTSIDE_WINDOW)

    codes = tuple(collector.codes)
    return ScheduleVerdict(
        ok=not codes,
        reason_codes=codes,
        failure_class=FAILURE_CLASS_IDIOSYNCRATIC if codes else None,
        evidence_flags=tuple(evidence),
        detail="; ".join(collector.details),
    )


def _merge_arm_verdicts(
    arm: str,
    records: Sequence[Mapping[str, Any]],
    *,
    session_window: SessionWindow,
    recompute_max_event_time: "Callable[[Mapping[str, Mapping[str, Any]]], Any] | None",
    expected_calendar_id: "str | None",
    expected_price_source_id: "str | None",
) -> "tuple[ScheduleVerdict, str | None]":
    """Merge one arm's records into a single verdict.

    Returns ``(verdict, declared_failure_kind)`` where the kind is
    non-None only when the arm produced NO qualifying record (all
    records declared failure — a crash-then-success retry is governed by
    its qualifying record, with the failed attempt kept as evidence).
    """
    qualifying = [r for r in records if _declared_failure(r) is None]
    failed = [r for r in records if _declared_failure(r) is not None]

    collector = _Collector()
    evidence: list[str] = []
    if failed and qualifying:
        evidence.append(EVIDENCE_FAILED_ATTEMPT_RECORDED)

    governing = qualifying or failed
    per_record = [
        validate_arm_record(
            r,
            session_window=session_window,
            recompute_max_event_time=recompute_max_event_time,
            expected_calendar_id=expected_calendar_id,
            expected_price_source_id=expected_price_source_id,
        )
        for r in governing
    ]
    for verdict in per_record:
        for code in verdict.reason_codes:
            collector.add(code, "")
        for flag in verdict.evidence_flags:
            if flag not in evidence:
                evidence.append(flag)
        if verdict.detail:
            collector.details.append(verdict.detail)

    if len(qualifying) > 1:
        # Retry byte-identity (v4 §2): every duplicate must agree on the
        # decision digest and the input digests — divergent duplicates
        # are an integrity failure, NEVER resolved by latest-commit.
        def _identity_bytes(r: Mapping[str, Any]) -> "tuple[Any, Any, Any]":
            manifest = r.get("input_manifest")
            input_digests = (
                tuple(
                    sorted(
                        (str(name), str((entry or {}).get("digest")))
                        for name, entry in manifest.items()
                        if isinstance(entry, Mapping)
                    )
                )
                if isinstance(manifest, Mapping)
                else None
            )
            return (r.get("job_id"), r.get("decision_digest"), input_digests)

        identities = {_identity_bytes(r) for r in qualifying}
        if len(identities) > 1:
            collector.add(
                REASON_DIVERGENT_RETRY,
                f"arm {arm!r} has {len(qualifying)} qualifying records with "
                f"{len(identities)} distinct decision/input identities — "
                "retries must be byte-identical and divergence is never "
                "resolved by selecting the latest commit",
            )

    failure_kind: "str | None" = None
    if failed and not qualifying:
        kinds = {
            str((_declared_failure(r) or {}).get("kind")) for r in failed
        }
        failure_kind = kinds.pop() if len(kinds) == 1 else "indeterminate"

    codes = tuple(collector.codes)
    non_failure_codes = tuple(
        c for c in codes if c != REASON_ARM_DECLARED_FAILURE
    )
    verdict = ScheduleVerdict(
        ok=not codes,
        reason_codes=codes,
        failure_class=(
            FAILURE_CLASS_IDIOSYNCRATIC if non_failure_codes else None
        ),
        evidence_flags=tuple(evidence),
        detail="; ".join(d for d in collector.details if d),
    )
    return verdict, failure_kind


def validate_session_records(
    records: Sequence[Mapping[str, Any]],
    *,
    session_window: SessionWindow,
    expected_arms: Sequence[str] = EXPECTED_ARMS,
    recompute_max_event_time: "Callable[[Mapping[str, Mapping[str, Any]]], Any] | None" = None,
    expected_calendar_id: "str | None" = None,
    expected_price_source_id: "str | None" = None,
) -> SessionVerdict:
    """Validate all records of ONE decision session (v4 §2).

    Session-level integrity on top of :func:`validate_arm_record`:
    every expected arm present, exactly one canonical job identity per
    arm (retries byte-identical), declared failures classified by arm
    symmetry (classification OUTPUT only — budget accounting is the
    runner's job). Never raises on a validation outcome.
    """
    collector = _Collector()
    evidence: list[str] = []

    parsable = [r for r in records if isinstance(r, Mapping)]
    if len(parsable) != len(records) or not records:
        collector.add(
            REASON_FIELD_INVALID,
            "session records must be a non-empty sequence of record mappings",
        )

    sessions = {str(r.get("decision_session")) for r in parsable}
    if len(sessions) > 1:
        collector.add(
            REASON_SESSION_MISMATCH,
            "records span multiple decision sessions "
            f"{sorted(sessions)} — one session per validation call",
        )

    by_arm: dict[str, list[Mapping[str, Any]]] = {}
    for r in parsable:
        by_arm.setdefault(str(r.get("arm")), []).append(r)

    for arm in sorted(set(by_arm) - set(expected_arms)):
        collector.add(
            REASON_UNEXPECTED_ARM,
            f"arm {arm!r} is not in the registered arm set {tuple(expected_arms)!r}",
        )

    arm_verdicts: list[tuple[str, ScheduleVerdict]] = []
    failed_kinds: dict[str, str] = {}
    for arm in expected_arms:
        arm_records = by_arm.get(arm, [])
        if not arm_records:
            collector.add(
                REASON_MISSING_ARM,
                f"expected arm {arm!r} has no record for this session "
                "(missing arm = integrity failure, v4 §2)",
            )
            continue
        verdict, failure_kind = _merge_arm_verdicts(
            arm,
            arm_records,
            session_window=session_window,
            recompute_max_event_time=recompute_max_event_time,
            expected_calendar_id=expected_calendar_id,
            expected_price_source_id=expected_price_source_id,
        )
        arm_verdicts.append((arm, verdict))
        if failure_kind is not None:
            failed_kinds[arm] = failure_kind
        for code in verdict.reason_codes:
            if code != REASON_ARM_DECLARED_FAILURE:
                collector.add(code, "")
        for flag in verdict.evidence_flags:
            evidence.append(f"{arm}:{flag}")
        if verdict.detail:
            collector.details.append(f"{arm}: {verdict.detail}")

    # Declared-failure classification (v4 §2 r2): symmetric shared-kind
    # failure -> shared (B_shared class); anything else -> idiosyncratic
    # (B_idio class). Classification OUTPUT only.
    shared = False
    if failed_kinds:
        kinds = set(failed_kinds.values())
        symmetric = set(failed_kinds) == set(expected_arms) and len(kinds) == 1
        if symmetric and next(iter(kinds)) in SHARED_FAILURE_KINDS:
            shared = True
            kind = next(iter(kinds))
            collector.add(
                REASON_SHARED_PRICE_SOURCE_FAILURE
                if kind == FAILURE_KIND_PRICE_SOURCE
                else REASON_SHARED_OUTAGE_FAILURE,
                f"both arms declared the same shared failure kind {kind!r} "
                "(symmetric -> B_shared class)",
            )
        else:
            collector.add(
                REASON_ASYMMETRIC_ARM_FAILURE,
                "declared arm failures "
                f"{dict(sorted(failed_kinds.items()))!r} are not a symmetric "
                "shared outage (asymmetric -> B_idio class)",
            )

    codes = tuple(collector.codes)
    return SessionVerdict(
        ok=not codes,
        reason_codes=codes,
        failure_class=(
            None
            if not codes
            else FAILURE_CLASS_SHARED
            if shared
            else FAILURE_CLASS_IDIOSYNCRATIC
        ),
        evidence_flags=tuple(evidence),
        arm_verdicts=tuple(arm_verdicts),
        detail="; ".join(d for d in collector.details if d),
    )


__all__ = [
    "ARM_CHAMPION",
    "ARM_L1",
    "DECISION_SCHEDULE_CONTRACT_VERSION",
    "EVIDENCE_FAILED_ATTEMPT_RECORDED",
    "EVIDENCE_TIMESTAMP_OUTSIDE_WINDOW",
    "EXPECTED_ARMS",
    "FAILURE_CLASS_IDIOSYNCRATIC",
    "FAILURE_CLASS_SHARED",
    "FAILURE_KIND_CALENDAR_OUTAGE",
    "FAILURE_KIND_FILL",
    "FAILURE_KIND_JOB_CRASH",
    "FAILURE_KIND_PRICE_SOURCE",
    "FAILURE_KIND_VALUATION",
    "FAILURE_KIND_VENUE_OUTAGE",
    "JOB_IDENTITY_SCHEMA",
    "KNOWN_FAILURE_KINDS",
    "QUALIFYING_RECORD_FIELDS",
    "REASON_ARM_DECLARED_FAILURE",
    "REASON_ASYMMETRIC_ARM_FAILURE",
    "REASON_DIVERGENT_RETRY",
    "REASON_FIELD_INVALID",
    "REASON_FIELD_MISSING",
    "REASON_FROZEN_IDENTIFIER_MISMATCH",
    "REASON_JOB_IDENTITY_MISMATCH",
    "REASON_MISSING_ARM",
    "REASON_ORDERS_NOT_NEXT_OPEN",
    "REASON_RECORD_SCHEMA_UNSUPPORTED",
    "REASON_SESSION_MISMATCH",
    "REASON_SHARED_OUTAGE_FAILURE",
    "REASON_SHARED_PRICE_SOURCE_FAILURE",
    "REASON_UNEXPECTED_ARM",
    "REASON_WATERMARK_AFTER_CLOSE",
    "REASON_WATERMARK_RECOMPUTE_MISMATCH",
    "RECORD_SCHEMA_VERSION",
    "SHARED_FAILURE_KINDS",
    "ScheduleVerdict",
    "SessionVerdict",
    "SessionWindow",
    "job_identity",
    "recompute_watermark_from_manifest",
    "validate_arm_record",
    "validate_session_records",
]
