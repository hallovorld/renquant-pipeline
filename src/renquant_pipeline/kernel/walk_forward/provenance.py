"""WF sim-time provenance sink — ``wf_sim_provenance.v1`` (design #215).

Implements the persistence half of ``doc/design/2026-07-27-wf-sim-provenance-
contract.md``: an append-only JSONL sink plus the record constructors for the
two record kinds, so a walk-forward sim persists WHICH fold/artifact scored
WHICH date at generation time instead of leaving the Phase-A converter to
reconstruct it post hoc (the inadmissible pattern the codex reviews on
model#64/#65/#66 rejected).

Contract summary (see the design doc for the authoritative table):

* ``schema_version: "wf_sim_provenance.v1"``; two record kinds keyed by
  ``(sim_run_id, prediction_date)``:

  - ``fold_resolved`` — emitted at the loader boundary when the fold that
    will serve scoring is resolved (``WalkForwardModelLoader.entry_as_of``).
  - ``score_committed`` — emitted at the persistence commit point after the
    per-date score observation is INSERTed
    (``RecordScoreDistributionTask.run``), binding the provenance to the
    exact observation Phase-A will read.

* Digest grammar: the FULL admissibility-ledger form ``sha256:<64 hex>``
  (``LABEL_REF_RE`` family) — deliberately NOT the #211 16-hex abbreviated
  observer form; converters may abbreviate downstream, the producer never
  does (the 2026-07-26 strategy#66 incident is the cautionary tale).

* ``score_timestamp`` is REQUIRED on ``score_committed`` and carries the
  SIMULATED session's decision instant for the bar (the
  ``decision_schedule.run_bundle_timestamp`` convention — timezone-aware
  ISO-8601). ``emitted_at_utc`` on both kinds is audit-write clock ONLY and
  is never a decision-time claim.

* PIT invariant enforced at record construction: ``input_watermark <=
  score_timestamp``. A breach STILL produces a record (append-only honesty)
  with ``pit_violation: true``; extraction rejects the date.

* Durability: ``data/wf_provenance/<sim_run_id>.jsonl`` beside the run
  bundle — never a ``sim_runs.db`` table (that DB is truncated every
  ``run_backtest``). Each row is fsync'd.

Live-surface delta: ZERO. Nothing in this module is constructed by the
daily path; the loader's ``provenance_sink`` default is ``None`` and the
score-distribution task no-ops unless the sim adapter stamped the ctx.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

SCHEMA_VERSION = "wf_sim_provenance.v1"
RECORD_KIND_FOLD_RESOLVED = "fold_resolved"
RECORD_KIND_SCORE_COMMITTED = "score_committed"
RECORD_KINDS = (RECORD_KIND_FOLD_RESOLVED, RECORD_KIND_SCORE_COMMITTED)

#: Canonical directory name under the sim data root (design §2.4:
#: ``data/wf_provenance/<sim_run_id>.jsonl``).
PROVENANCE_DIRNAME = "wf_provenance"

#: Full digest grammar, verbatim from the admissibility ledger's
#: ``DIGEST_RE``/``LABEL_REF_RE`` family (the consumer is the admissibility
#: chain, so we use its grammar end-to-end — design §2.2).
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Fixed per-row field order of the canonical score-payload serialization
#: (design §2.1 "binding" group). Changing this breaks digest equality with
#: every consumer — do not reorder.
SCORE_PAYLOAD_FIELDS = ("ticker", "raw_panel", "mu", "rank_score", "sigma")


class ProvenanceSink(Protocol):
    """The single small protocol shared by both emit sites (design §2.3)."""

    def emit(self, record: dict) -> None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------

def sha256_digest(data: bytes) -> str:
    """``sha256:<64 hex>`` over ``data`` — the one producer-side grammar."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def file_digest(path: "str | Path") -> "str | None":
    """Whole-file-bytes digest, or ``None`` when the file does not exist."""
    p = Path(path)
    if not p.is_file():
        return None
    return sha256_digest(p.read_bytes())


def _canonical_value(value: Any) -> "str | None":
    """Float canonicalization: ``repr(float(v))`` — ints normalize through
    float so ``1`` and ``1.0`` serialize identically; ``None`` stays null."""
    if value is None:
        return None
    return repr(float(value))


def canonical_score_payload(rows: Iterable[Mapping[str, Any]]) -> bytes:
    """Canonical serialization of a persisted score series (design §2.1).

    ``rows`` are mappings carrying at least :data:`SCORE_PAYLOAD_FIELDS`.
    Rules (extraction recomputes over what it reads back and requires
    byte-equality, so every rule here is load-bearing):

    * rows sorted by ``str(ticker)``;
    * fixed field order ``(ticker, raw_panel, mu, rank_score, sigma)``;
    * numeric values via ``repr(float(v))`` (``None`` → JSON null);
    * one compact JSON array per row, newline-joined, UTF-8.
    """
    lines = []
    for row in sorted(rows, key=lambda r: str(r["ticker"])):
        lines.append(json.dumps(
            [str(row["ticker"])]
            + [_canonical_value(row.get(f)) for f in SCORE_PAYLOAD_FIELDS[1:]],
            separators=(",", ":"),
            ensure_ascii=True,
        ))
    return "\n".join(lines).encode("utf-8")


def score_payload_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    """``sha256:<64 hex>`` over :func:`canonical_score_payload`."""
    return sha256_digest(canonical_score_payload(rows))


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_aware(value: str, *, field_name: str) -> dt.datetime:
    """Parse a timezone-AWARE ISO-8601 timestamp; naive/unparsable fail loud.

    Mirrors the ``decision_schedule.run_bundle_timestamp`` validation rule:
    audit/decision timestamps must carry an offset — a naive timestamp is a
    latent PIT-comparison bug, not a convenience.
    """
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be an ISO-8601 timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware (ISO-8601 with offset), "
            f"got {value!r}"
        )
    return parsed


def utc_now_iso() -> str:
    """Audit-write clock (``emitted_at_utc``) — NEVER a decision-time claim."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Record constructors
# ---------------------------------------------------------------------------

def _require_digest(value: Any, *, field_name: str) -> str:
    text = str(value)
    if not DIGEST_RE.match(text):
        raise ValueError(
            f"{field_name} must use the full digest grammar "
            f"sha256:<64 hex>, got {value!r}"
        )
    return text


def _optional_digest(value: Any, *, field_name: str) -> "str | None":
    if value is None:
        return None
    return _require_digest(value, field_name=field_name)


def build_fold_resolved_record(
    *,
    prediction_date: str,
    cutoff_date: str,
    trained_date: str,
    effective_train_cutoff_date: "str | None",
    lookahead_days: int,
    artifact_uri: str,
    calibrator_uri: "str | None",
    manifest_path: str,
    manifest_digest: str,
    artifact_digest: "str | None",
    is_real_content_digest: bool,
    family: str,
    fingerprint_schema: str,
    calibrator_digest: "str | None" = None,
    sim_run_id: "str | None" = None,
    seed: "int | None" = None,
    revision_pins: "Mapping[str, Any] | None" = None,
) -> dict:
    """``record_kind: "fold_resolved"`` per design §2.1.

    ``sim_run_id`` / ``seed`` / ``revision_pins`` may be left ``None`` when
    the emitter (the loader) does not hold them in scope — the JSONL sink
    completes them from its construction args (the run_backtest identity).
    """
    if is_real_content_digest and artifact_digest is None:
        raise ValueError(
            "fold_resolved: is_real_content_digest=True requires an "
            "artifact_digest"
        )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND_FOLD_RESOLVED,
        # identity (sink-completable)
        "sim_run_id": sim_run_id,
        "prediction_date": str(prediction_date),
        "seed": seed,
        # fold — the selected RetrainEntry
        "cutoff_date": str(cutoff_date),
        "trained_date": str(trained_date),
        "effective_train_cutoff_date": (
            None if effective_train_cutoff_date is None
            else str(effective_train_cutoff_date)
        ),
        "lookahead_days": int(lookahead_days),
        "artifact_uri": str(artifact_uri),
        "calibrator_uri": None if calibrator_uri is None else str(calibrator_uri),
        # manifest
        "manifest_path": str(manifest_path),
        "manifest_digest": _require_digest(
            manifest_digest, field_name="manifest_digest",
        ),
        # artifact
        "artifact_digest": _optional_digest(
            artifact_digest, field_name="artifact_digest",
        ),
        "is_real_content_digest": bool(is_real_content_digest),
        "family": str(family),
        "fingerprint_schema": str(fingerprint_schema),
        # calibrator
        "calibrator_digest": _optional_digest(
            calibrator_digest, field_name="calibrator_digest",
        ),
        # code (sink-completable)
        "revision_pins": None if revision_pins is None else dict(revision_pins),
    }
    return record


def build_score_committed_record(
    *,
    prediction_date: str,
    score_observation_key: Sequence[Any],
    score_payload_digest: str,
    n_rows: int,
    artifact_digest: "str | None",
    score_timestamp: str,
    input_watermark: "str | None" = None,
    persisted: bool = True,
    sim_run_id: "str | None" = None,
) -> dict:
    """``record_kind: "score_committed"`` per design §2.1/§2.2.

    ``score_timestamp`` is REQUIRED — it is the simulated session's decision
    instant for the bar, the field that certifies a historical decision when
    the rerun happens today. The PIT invariant ``input_watermark <=
    score_timestamp`` is checked HERE: a breach still returns a record
    (append-only honesty) with ``pit_violation: true``; extraction rejects
    the date. ``input_watermark=None`` means the ctx data axis did not
    declare a watermark — recorded as null, never treated as a pass of the
    check it couldn't run (extraction owns that judgement).
    """
    if not score_timestamp:
        raise ValueError(
            "score_committed: score_timestamp is REQUIRED (design §2.2) — "
            "the simulated decision instant, not the audit clock"
        )
    score_ts = _parse_aware(score_timestamp, field_name="score_timestamp")
    pit_violation = False
    watermark_iso: "str | None" = None
    if input_watermark is not None:
        watermark = _parse_aware(input_watermark, field_name="input_watermark")
        watermark_iso = str(input_watermark)
        pit_violation = watermark > score_ts
    key = list(score_observation_key)
    if len(key) != 3:
        raise ValueError(
            "score_committed: score_observation_key must be the "
            "score_distribution primary-key coordinates (run_id, date, "
            f"run_type), got {score_observation_key!r}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "record_kind": RECORD_KIND_SCORE_COMMITTED,
        # join (sim_run_id sink-completable)
        "sim_run_id": sim_run_id,
        "prediction_date": str(prediction_date),
        "score_observation_key": key,
        # binding
        "score_payload_digest": _require_digest(
            score_payload_digest, field_name="score_payload_digest",
        ),
        "n_rows": int(n_rows),
        # pairing — direct echo of the fold_resolved value
        "artifact_digest": _optional_digest(
            artifact_digest, field_name="artifact_digest",
        ),
        # time
        "score_timestamp": str(score_timestamp),
        # inputs
        "input_watermark": watermark_iso,
        "pit_violation": bool(pit_violation),
        # durability
        "persisted": bool(persisted),
    }


# ---------------------------------------------------------------------------
# Revision pins (design §2.1 "code" group)
# ---------------------------------------------------------------------------

def capture_revision_pins(
    repos: Mapping[str, "str | Path"],
) -> dict[str, "str | None"]:
    """Best-effort multi-repo HEAD pin capture (``name -> sha | None``).

    DESIGN-VS-CODE NOTE: the design names
    ``pit_parity_ledger.commit_path_fingerprint`` as the pin-capture to
    reuse; no such function exists in any repo (the pit_parity_ledger is a
    pure-data comparator — its docstring delegates pin capture to the
    umbrella harness). This helper is the pipeline-side stand-in: the sim
    adapter (umbrella follow-up) passes the sibling-checkout map and hands
    the result to the sink at construction. Never the cwd-only
    ``persistence._commit_sha``.
    """
    import subprocess  # noqa: PLC0415 - keep module import-light

    pins: dict[str, "str | None"] = {}
    for name, path in repos.items():
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10, check=False,
            )
            sha = proc.stdout.strip()
            pins[name] = sha if proc.returncode == 0 and sha else None
        except (OSError, subprocess.SubprocessError):
            pins[name] = None
    return pins


# ---------------------------------------------------------------------------
# Append-only JSONL sink
# ---------------------------------------------------------------------------

#: Record keys excluded from the idempotency identity — the audit clock
#: differs across re-emits by construction and must not defeat the no-op.
_AUDIT_ONLY_KEYS = frozenset({"emitted_at_utc"})


class JsonlProvenanceSink:
    """Append-only JSONL writer, fsync per row (design §2.3/§2.4).

    ``directory`` is the provenance directory (canonically
    ``<data_root>/wf_provenance``); the file is ``<sim_run_id>.jsonl``
    inside it. The file is NEVER truncated — append-only across process
    restarts of the same ``sim_run_id``.

    Identity completion: records built where the run identity is out of
    scope (the loader) arrive with ``sim_run_id``/``seed``/``revision_pins``
    ``None``; ``emit`` fills them from construction args. A record carrying
    a DIFFERENT non-null ``sim_run_id`` is a cross-run mix-up and raises.

    Idempotency (in-sink dedup):

    * ``fold_resolved`` — deduped per ``(sim_run_id, prediction_date)``:
      re-entrant resolutions of the same bar (``model_as_of`` +
      ``calibrator_as_of`` both funnel through ``entry_as_of``) emit once.
      A SECOND resolution with DIFFERENT content for the same key is still
      appended (append-only honesty — extraction rejects the date as a
      duplicate rather than the sink masking a real double-resolution).
    * ``score_committed`` — re-emit of an identical row (audit clock
      excluded) is a no-op; differing content for the same key appends and
      is left to the extraction byte-identity rule.
    """

    def __init__(
        self,
        sim_run_id: str,
        directory: "str | Path",
        *,
        seed: "int | None" = None,
        revision_pins: "Mapping[str, Any] | None" = None,
    ) -> None:
        run_id = str(sim_run_id)
        if (not run_id or run_id in {".", ".."} or "/" in run_id
                or os.sep in run_id or (os.altsep and os.altsep in run_id)):
            raise ValueError(
                f"JsonlProvenanceSink: sim_run_id {sim_run_id!r} is not a "
                "safe filename component"
            )
        self._sim_run_id = run_id
        self._seed = seed
        self._revision_pins = None if revision_pins is None else dict(revision_pins)
        self._directory = Path(directory)
        self._path = self._directory / f"{run_id}.jsonl"
        #: record_kind -> key -> set of content identities already appended.
        self._seen: dict[str, dict[str, set[str]]] = {
            kind: {} for kind in RECORD_KINDS
        }

    @property
    def sim_run_id(self) -> str:
        return self._sim_run_id

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, record: dict) -> None:
        completed = self._complete(record)
        kind = completed["record_kind"]
        key = str(completed["prediction_date"])
        content_id = json.dumps(
            {k: v for k, v in completed.items() if k not in _AUDIT_ONLY_KEYS},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        seen_for_key = self._seen[kind].setdefault(key, set())
        if content_id in seen_for_key:
            return  # idempotent re-emit — no-op
        # NOTE: a same-key record with DIFFERENT content falls through and
        # is appended (append-only honesty) — extraction's duplicate /
        # byte-identity rules reject the date rather than the sink masking
        # a real double-resolution or re-score.
        completed["emitted_at_utc"] = utc_now_iso()
        self._append(completed)
        seen_for_key.add(content_id)

    # -- internals ---------------------------------------------------------

    def _complete(self, record: dict) -> dict:
        if not isinstance(record, Mapping):
            raise ValueError(
                f"provenance record must be a mapping, got {type(record).__name__}"
            )
        completed = dict(record)
        if completed.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"provenance record schema_version must be {SCHEMA_VERSION!r}, "
                f"got {completed.get('schema_version')!r}"
            )
        kind = completed.get("record_kind")
        if kind not in RECORD_KINDS:
            raise ValueError(
                f"provenance record_kind must be one of {RECORD_KINDS}, "
                f"got {kind!r}"
            )
        declared_run = completed.get("sim_run_id")
        if declared_run is None:
            completed["sim_run_id"] = self._sim_run_id
        elif str(declared_run) != self._sim_run_id:
            raise ValueError(
                f"provenance record sim_run_id {declared_run!r} does not match "
                f"this sink's {self._sim_run_id!r} — cross-run emit refused"
            )
        if not completed.get("prediction_date"):
            raise ValueError("provenance record requires prediction_date")
        if kind == RECORD_KIND_FOLD_RESOLVED:
            if completed.get("seed") is None:
                completed["seed"] = self._seed
            if completed.get("revision_pins") is None:
                completed["revision_pins"] = self._revision_pins
        return completed

    def _append(self, record: dict) -> None:
        line = json.dumps(
            record, sort_keys=True, separators=(",", ":"), default=str,
        )
        self._directory.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
