"""Shadow-scorer HEALTH RECORD — the canonical silent-failure contract.

This module is the SINGLE SOURCE OF TRUTH for three consumers that must never
drift apart:

  * ``ApplyShadowScoringTask`` (renquant-pipeline) — EMITS one health record per
    configured shadow model per run;
  * the shadow-artifact CI gate (orchestrator PR #525) — validates a configured
    shadow artifact resolves + carries identity at CI time;
  * the shadow-health sentinel (orchestrator PR #566) — tails the emitted JSONL
    and alarms on silent degradation.

Keeping the resolution + identity + verdict logic here (pure, stdlib-only apart
from the light ``kernel.artifact_resolver``) means the same ref resolves to the
same file, the same digest recipe stamps the same identity, and the same
expected-skip-vs-fault verdict is computed everywhere — no three independent
resolvers (the exact class of the 2026 shadow-dead-for-a-week incident #114).

WHY THIS EXISTS (the failure this guards): the shadow scorer is fail-soft — a
broken ``../../`` artifact_path makes it load-fail and CONTINUE, so a
G4-critical comparison feed can die for weeks with nothing but a per-run
``log.warning``. The record makes that VISIBLE without making the shadow fatal.

── ARTIFACT IDENTITY (not mere path existence) ────────────────────────────────
A path merely existing does NOT prove the artifact is the one scoring used: the
file at a mutable path can be swapped. ``content_sha256`` captures the IMMUTABLE
content identity of the file scoring loaded (changes the moment the bytes
change); ``config_fingerprint`` is the training-config identity stamped in the
artifact metadata. A shadow with absent required identity — or a mismatch
against a config-pinned ``expected_content_sha256`` / ``expected_config_fingerprint``
— is a FAULT, not healthy.

── EXPECTED-SKIP vs FAULT (the sentinel's decision axis) ──────────────────────
``status`` ∈ {``ok``, ``expected_skip``, ``fault``} and the boolean
``actionable`` (``actionable == status != "fault"``) are the sentinel contract:

  * ``ok``            — loaded + fresh + provenanced + covered → ``actionable=True``
  * ``expected_skip`` — intentionally not running this shadow this run (disabled,
                        no shadow models configured, or nothing to score) →
                        ``loaded=False`` yet ``actionable=True``. A by-design
                        non-load is NOT a fault; the sentinel must NOT alarm.
  * ``fault``         — a real setup/degradation problem (unresolved artifact,
                        load failure, stale cutoff, low coverage, absent/mismatched
                        identity) → ``actionable=False`` + ``reasons`` tokens.

The sentinel alarms iff, for a configured shadow, the latest record is a
``fault`` (or NO record was emitted) for ≥ N consecutive runs. ``expected_skip``
records keep the per-shadow timeline continuous so silence is unambiguous.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Bump ONLY on a breaking field/semantics change; the sentinel (#566) and CI
# gate (#525) gate their parse on this exact tag.
SHADOW_HEALTH_SCHEMA = "shadow_scorer_health.v1"

DEFAULT_SHADOW_HEALTH_RELPATH = Path("logs") / "shadow_scorer_health.jsonl"
# Freshness bar mirrors the model-freshness governance policy ("NO model
# > 28 days"); coverage bar mirrors the fundamentals min_coverage (0.80) used
# by DataAvailabilityTask. Both operator-overridable under config.shadow_health.
DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS = 28
DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC = 0.80

# ── Status buckets (the sentinel decision axis) ────────────────────────────────
STATUS_OK = "ok"
STATUS_EXPECTED_SKIP = "expected_skip"
STATUS_FAULT = "fault"

# ── State vocabulary (the precise sub-state; closed set) ───────────────────────
STATE_DISABLED = "disabled"                 # shadow scoring turned off (task-level)
STATE_NO_SHADOW_MODELS = "no_shadow_models"  # none configured (task-level)

#: The `shadow_name` used on a TASK-LEVEL skip — one that is about the task, not about
#: any lane (`shadow_enabled=false`, or no `shadow_models` configured at all).
#:
#: WHY IT IS NOT `None`. The consumer's strict parser
#: (`rq104_shadow_scorer_sentinel.is_valid_v1_record`) requires
#: `isinstance(shadow_name, str)` and IGNORES the whole record otherwise. Measured
#: 2026-07-31 on the live log: 12 `degraded` rows parsed, and **4 `no_shadow_models`
#: rows were discarded** — so the `expected_skip` status this module defines had never
#: been exercised for them. The producer was emitting a record its own consumer refuses
#: by definition.
#:
#: The consumer is right to demand it: a record that cannot be attributed to a lane is
#: not evidence about a lane. The fix belongs here — say WHICH lane, even when the
#: answer is "none, this is about the task".
#:
#: Deliberately not a plausible lane name. A real lane is named in config; this must be
#: unmistakably a sentinel so no reader mistakes it for a configured shadow.
TASK_LEVEL_SHADOW_NAME = "__task_level__"
STATE_NO_CANDIDATES = "no_candidates"        # nothing to score this run (per-model)
#: A ledger-pointer lane whose artifact_path RESOLVES but whose ledger carries
#: ZERO rows yet — the designed pre-first-publish window (model#197 amendment 2,
#: the s104 PENDING_FIRST_ARTIFACT guard). Distinct from ``unresolved_artifact``
#: (the ref points nowhere — the ``../../`` fault class) and from ``load_failed``
#: (any other refusal: a verification check failed, or a certified ledger
#: disappeared before the loader's read — pipeline#254): this is the
#: one absent-state that is BY DESIGN, so it is an expected skip, not a fault —
#: the same tri-state discipline as ok / expected_skip / fault itself. Additive
#: state within the existing status buckets: the deployed sentinel constrains
#: ``status`` to the canonical three and requires ``state`` only to be a string
#: (ops/renquant104/rq104_shadow_scorer_sentinel.py::is_valid_v1_record), so no
#: schema bump.
STATE_NOT_YET_PUBLISHED = "not_yet_published"
STATE_OK = "ok"                              # loaded + fresh + provenanced + covered
STATE_DEGRADED = "degraded"                  # loaded but stale/low-cov/identity issue
STATE_NOT_SCORED = "not_scored"              # loaded but produced no usable scores
STATE_UNRESOLVED_ARTIFACT = "unresolved_artifact"  # ref did not resolve (../.. class)
STATE_LOAD_FAILED = "load_failed"            # resolved but scorer_loader raised

EXPECTED_SKIP_STATES = frozenset({
    STATE_DISABLED, STATE_NO_SHADOW_MODELS, STATE_NO_CANDIDATES,
    STATE_NOT_YET_PUBLISHED,
})
FAULT_STATES = frozenset({
    STATE_DEGRADED, STATE_NOT_SCORED, STATE_UNRESOLVED_ARTIFACT, STATE_LOAD_FAILED,
})

class ShadowNotYetPublished(Exception):
    """Raised by a kind handler whose configured artifact is a LEDGER POINTER
    that resolves to a real file carrying zero published rows yet.

    This is the designed PENDING_FIRST_ARTIFACT window (model#197 amendment 2):
    the s104 config entry pins the cutoff-stable ledger, and until the weekly
    train job's first publish the ledger legitimately has no tail row to serve.
    ``ApplyShadowScoringTask`` catches this exception SPECIFICALLY — before its
    generic load-failure handler — and stamps ``STATE_NOT_YET_PUBLISHED`` as an
    expected skip. Every OTHER loader exception remains a recorded FAULT —
    including a certified ledger that DISAPPEARS before the loader's read: the
    task gates on ``identity.resolved`` first, so only a successfully read,
    chain-verified EMPTY ledger may raise this (pipeline#254)."""


# Provenance / identity metadata field names read off the loaded scorer.
TRAIN_CUTOFF_FIELD = "effective_train_cutoff_date"
CONFIG_FINGERPRINT_FIELD = "config_fingerprint"

#: Axis-1 field: when the model was TRAINED. This is what the freshness
#: governance policy ("no model older than 28 days") actually governs, and it
#: is fully controllable by retrain cadence.
TRAINED_DATE_FIELD = "trained_date"

#: Axis-2 input: the recipe's label horizon in TRADING days, read per-artifact.
#: Never hardcoded — a fwd20 and a fwd60 recipe have different structural
#: floors, and guessing one for the other is how a gate becomes unsatisfiable.
LOOKAHEAD_FIELD = "lookahead_days"

#: Calendar slack allowed on top of the structural floor in axis 2. Absorbs
#: holiday variance and gives the retrain cadence margin. Deliberately equal to
#: the axis-1 ceiling so the two axes stay legible together.
DEFAULT_CUTOFF_LAG_SLACK_DAYS = 28

#: Sanity bound on a self-declared horizon: a recipe claiming a horizon beyond
#: ~one trading year does not get an unbounded freshness allowance.
MAX_DECLARED_LOOKAHEAD_TDAYS = 252

CONTENT_SHA256_PREFIX = "sha256:"


# ── Frozen forward readouts (2026-09-03) ───────────────────────────────────────
#
# A shadow lane whose artifact is FROZEN BY DESIGN — a certified model accruing
# a fixed-length forward ledger (pipeline#213: 60-session INFO read, 120-session
# GATE) — cannot be "retrained" without voiding the readout, so the two-axis
# freshness rule (trained_date age, cutoff lag) fires on it every session for
# the whole readout window. Measured 2026-09-01..03 on the live sentinel log:
# `topdecile_clf_blend_leg` DEGRADED daily with `cutoff_lag_128d_over_112d`,
# `trained_37d_limit_28d` — a page that says "retrain me" about a lane whose
# whole value is that it is NOT retrained.
#
# The exemption is bound to the ARTIFACT, not the lane name: it applies only
# while the record's observed ``content_sha256`` AND the config-pinned
# ``expected_content_sha256`` both equal the certified digest, and only until
# ``until`` (self-expiring, like the RFC#210 A4-T1 license). A swapped
# artifact, a config that stops pinning it, or the calendar running out all
# return the lane to the standing rule with no code change. Only the
# FRESHNESS tokens are suppressed — identity mismatch, missing provenance,
# low coverage and no-scores remain faults — and the suppressed tokens are
# written into the record (``frozen_forward_readout.freshness_suppressed``)
# so the staleness stays visible to a reader, just not as an alarm.

@dataclass(frozen=True)
class FrozenForwardReadout:
    lane: str                 # shadow_name (exact, or '<lane>_<suffix>')
    content_sha256: str       # certified artifact digest (16-hex prefix or full)
    frozen_since: datetime.date
    until: datetime.date      # inclusive; after this the standing rule applies
    authority: str

    def matches_lane(self, shadow_name: Any) -> bool:
        name = str(shadow_name or "")
        return name == self.lane or name.startswith(self.lane + "_")


FROZEN_FORWARD_READOUTS: tuple[FrozenForwardReadout, ...] = (
    FrozenForwardReadout(
        lane="topdecile_clf_blend_leg",
        content_sha256="1e644354e0981f47",
        frozen_since=datetime.date(2026, 7, 27),
        # 120 sessions from 2026-07-27 plus the 60-session label maturity the
        # GATE read needs lands ~Feb 2027 (pipeline#213 schedule); one month
        # of slack for holidays and the readout job's own cadence.
        until=datetime.date(2027, 3, 31),
        authority=("pipeline#213 frozen forward readout (design doc "
                   "2026-07-25-blend-shadow-deployment.md); strategy-104 config "
                   "pins the artifact via expected_content_sha256 and records "
                   "the role in _2026_07_26_role"),
    ),
)

#: Freshness reason tokens a frozen forward readout suppresses. Provenance
#: defects (missing / unparseable / future dates) are NOT freshness and stay.
_FROZEN_SUPPRESSIBLE = (
    re.compile(r"^cutoff_lag_\d+d_over_\d+d"),
    re.compile(r"^stale_\d+d_limit_\d+d$"),
    re.compile(r"^trained_\d+d_limit_\d+d$"),
    re.compile(r"^no_declared_lookahead_single_axis$"),
)

#: Config key under which the live runner stamps the RESOLVED path of the
#: strategy config file it loaded (``live/runner.py`` — set alongside
#: ``_strategy_dir`` / ``_strategy_config_name``). This is the one place the
#: run already knows WHICH config file it is running under, so the health
#: record's task-config identity reads it rather than re-deriving (and
#: possibly mis-guessing) a path.
STRATEGY_CONFIG_PATH_KEY = "_strategy_config_path"

# Per-process content-digest cache for the standalone ``content_digest`` helper,
# keyed by (path, mtime_ns, size) so a 700-bar sim does not re-hash a 100 MB
# checkpoint every bar. NOTE: the (path, mtime, size) key is a PERFORMANCE
# heuristic, not an identity guarantee — a same-size, mtime-preserving swap would
# reuse the cached digest. The AUTHORITATIVE identity the health record certifies
# is stamped from ``resolve_artifact_identity`` (which reads the resolved file's
# bytes directly via ``kernel.artifact_resolver``, NOT this cache), so do not rely
# on this cache for swap detection.
_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


# ── Canonical artifact resolution + content identity ───────────────────────────

@dataclass(frozen=True)
class ArtifactIdentity:
    """Immutable identity of a resolved shadow artifact.

    ``content_sha256`` is ``sha256:<16 hex>`` of the file bytes — the SAME
    digest recipe ``kernel.artifact_resolver`` feeds into the run fingerprint,
    so a swapped file is always observable. ``None`` when the ref did not
    resolve. ``source`` ∈ {``absolute``, ``strategy_dir``, ``repo_root``,
    ``unresolved``}.
    """
    ref: str | None
    resolved: bool
    resolved_path: str | None
    source: str
    content_sha256: str | None
    error: str | None


def content_digest(path: str | Path | None) -> str | None:
    """``sha256:<16 hex>`` content identity of a file, or None if unreadable.

    Canonical digest recipe (matches ``kernel.artifact_resolver``). This is the
    IMMUTABLE identity of the artifact ACTUALLY used by scoring — hashing the
    resolved path scoring loaded, so replacing the file at a mutable path
    changes the digest and the drift is caught."""
    if path is None:
        return None
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    if not Path(path).is_file():
        return None
    key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        digest = CONTENT_SHA256_PREFIX + hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None
    _DIGEST_CACHE[key] = digest
    return digest


def task_config_identity(config: dict) -> "tuple[str | None, str | None]":
    """``(task_config_path, task_config_sha256)`` — the identity of the
    STRATEGY CONFIG file the emitting task ran under (issue #256).

    The shadow-health sink receives records from MULTIPLE invocations per
    session (the main daily run plus companion profiles such as shadow_blend).
    A task-level ``no_shadow_models`` record with no config identity cannot be
    told apart from "the MAIN config dropped all shadow lanes" — the exact
    alarm class the record exists to make legible — so EVERY record is stamped
    with the config file the task actually ran under.

    Reads the resolved path the runner stamped at ``STRATEGY_CONFIG_PATH_KEY``
    and hashes the file with the canonical ``content_digest`` recipe
    (``sha256:<16 hex>`` — the SAME convention the record's artifact
    ``content_sha256`` uses, so the sentinel compares one digest form).

    FAIL CLOSED: when the runner did not stamp a path, returns ``(None,
    None)`` rather than guessing ``<strategy_dir>/strategy_config.json`` — a
    guessed default could stamp the MAIN config's identity onto a companion
    profile's record, recreating the very false-attribution vector this field
    kills. An absent identity stays legibly absent."""
    path = (config or {}).get(STRATEGY_CONFIG_PATH_KEY)
    if not path:
        return None, None
    return str(path), content_digest(str(path))


def resolve_artifact_identity(
    ref: str | Path | None,
    *,
    strategy_dir: str | Path | None,
    repo_root: str | Path | None = None,
) -> ArtifactIdentity:
    """Canonical, PURE resolve-a-ref-to-one-file + stamp its content identity.

    Delegates path resolution to ``kernel.artifact_resolver`` — the established
    ONE resolution authority (absolute → strategy_dir → repo_root) — so the
    health record, the CI gate (#525) and the sentinel (#566) all turn a ref
    into the same file and the same digest. Never raises."""
    ref_s = None if ref is None else str(ref)
    if not ref_s:
        return ArtifactIdentity(ref_s, False, None, "unresolved",
                                None, "missing artifact_path ref")
    if strategy_dir is None:
        return ArtifactIdentity(ref_s, False, None, "unresolved",
                                None, "strategy_dir not configured")
    from renquant_pipeline.kernel.artifact_resolver import (  # noqa: PLC0415
        locate_artifact, resolve_artifact,
    )
    rr = Path(repo_root) if repo_root is not None else None
    try:
        ra = resolve_artifact(ref_s, strategy_dir=Path(strategy_dir), repo_root=rr)
    except FileNotFoundError as exc:
        try:
            loc = str(locate_artifact(
                ref_s, strategy_dir=Path(strategy_dir), repo_root=rr))
        except Exception:  # noqa: BLE001 — best-effort expected-path label
            loc = None
        return ArtifactIdentity(ref_s, False, loc, "unresolved", None, str(exc))
    except Exception as exc:  # noqa: BLE001 — resolver precondition (bad strategy_dir)
        return ArtifactIdentity(ref_s, False, None, "unresolved", None, str(exc))
    return ArtifactIdentity(
        ref_s, True, str(ra.path), ra.source,
        CONTENT_SHA256_PREFIX + ra.sha256, None)


# ── Cutoff parsing ─────────────────────────────────────────────────────────────

def _parse_cutoff_date(value: Any) -> datetime.date | None:
    """Parse a ``YYYY-MM-DD`` cutoff stamp (leading 10 chars). None if
    absent/unparseable — mirrors job_universe._axis_cutoff parsing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ── Record construction + verdict ──────────────────────────────────────────────

def new_shadow_health(
    *, shadow_name: Any, kind: Any, artifact_path: Any,
    run_date: datetime.date, run_id: Any, n_candidates: int,
    expected_content_sha256: Any = None,
    expected_config_fingerprint: Any = None,
    task_config_path: Any = None,
    task_config_sha256: Any = None,
) -> dict[str, Any]:
    """A health record pre-seeded to the WORST case (nothing loaded/scored).

    Fields are filled progressively by the task; ``finalize_shadow_health``
    then derives ``state`` / ``status`` / ``actionable`` / ``reasons``. Every
    field is present (null when unknown) so the schema is stable for the
    sentinel parser."""
    return {
        "schema": SHADOW_HEALTH_SCHEMA,
        "run_date": run_date.isoformat(),
        "run_id": str(run_id) if run_id is not None else None,
        # Identity of the STRATEGY CONFIG the emitting task ran under (#256):
        # the resolved config file path + its content digest (see
        # ``task_config_identity``). Run-level, stamped identically on
        # task-level AND per-lane records, so a task-level `no_shadow_models`
        # written by a companion profile (shadow_blend) is attributable to
        # THAT profile, not mistaken for the main config dropping its lanes.
        # NOT the same object as `config_fingerprint` below — that is the
        # ARTIFACT's training-config fingerprint read off the loaded scorer's
        # metadata. Additive v1 fields (no schema bump — the trained_date /
        # n_scored_total precedent): readers that ignore them are unaffected.
        "task_config_path": str(task_config_path) if task_config_path else None,
        "task_config_sha256": (
            str(task_config_sha256) if task_config_sha256 else None),
        "shadow_name": shadow_name,
        "kind": kind,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "artifact_resolved": False,
        "artifact_resolved_path": None,
        "artifact_source": None,
        # Immutable identity of the artifact actually used by scoring.
        "content_sha256": None,
        "config_fingerprint": None,
        # Optional config-pinned expected identity (mismatch → fault).
        "expected_content_sha256": (
            str(expected_content_sha256) if expected_content_sha256 else None),
        "expected_config_fingerprint": (
            str(expected_config_fingerprint) if expected_config_fingerprint else None),
        "loaded": False,
        "load_error": None,
        TRAIN_CUTOFF_FIELD: None,
        "staleness_days": None,
        "n_candidates": int(n_candidates),
        # n_scored counts finite shadow scores WITHIN the candidate set (the
        # coverage numerator); n_scored_total counts every finite score the
        # shadow produced, so a shadow scoring a wider matrix than the
        # candidates stays observable without pushing a fraction past 1.0.
        "n_scored": 0,
        "n_scored_total": 0,
        "coverage_frac": None,
        "skip_reason": None,
        "state": None,
        "status": None,
        "actionable": False,
        "reasons": [],
    }


def _set_status(health: dict[str, Any], state: str, reasons: list[str]) -> dict[str, Any]:
    health["state"] = state
    health["reasons"] = reasons
    if state in FAULT_STATES:
        health["status"] = STATUS_FAULT
        health["actionable"] = False
    elif state in EXPECTED_SKIP_STATES:
        health["status"] = STATUS_EXPECTED_SKIP
        health["actionable"] = True
    else:  # STATE_OK
        health["status"] = STATUS_OK
        health["actionable"] = True
    return health


def mark_expected_skip(health: dict[str, Any], state: str, reason: str | None = None) -> dict[str, Any]:
    """Stamp a by-design non-run (disabled / no models / no candidates) as an
    EXPECTED skip: ``actionable=True``, ``status=expected_skip``. Used for the
    task-level early paths so a record is emitted BEFORE every early return and
    the sentinel can tell an expected skip from a fault (or from silence)."""
    if state not in EXPECTED_SKIP_STATES:
        raise ValueError(f"{state!r} is not an expected-skip state")
    health["staleness_days"] = None
    return _set_status(health, state, [reason or state])


def _trading_to_calendar_days(trading_days: int) -> int:
    """Trading days -> calendar days, the plain 5-day-week conversion.

    Deliberately arithmetic rather than calendar-aware: a holiday-exact bound
    would make the gate depend on which holidays fall inside the window, and
    the SLACK term exists precisely to absorb that variance. Rounding up keeps
    the bound conservative in the direction that avoids false alarms.
    """
    weeks, rem = divmod(int(trading_days), 5)
    return weeks * 7 + rem + (2 if rem else 0)


def _declared_lookahead(health: dict[str, Any]) -> int | None:
    """The artifact's own label horizon in trading days, or None.

    None means FAIL CLOSED to the single-axis rule — never a guessed default.
    A wrong horizon is worse than no horizon: it silently widens the gate for
    a recipe that did not earn the widening.
    """
    raw = health.get(LOOKAHEAD_FIELD)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > MAX_DECLARED_LOOKAHEAD_TDAYS:
        return None
    return v


def _freshness_reasons(
    health: dict[str, Any], *, run_date: datetime.date, cutoff_lag_days: int,
    max_staleness_days: int, cutoff_lag_slack_days: int,
) -> list[str]:
    """Two-axis freshness (GOAL-6 decision A, 2026-07-29).

    The single-axis rule measured `run_date - effective_train_cutoff_date`
    against 28 calendar days. For a fwd60 recipe that bound is UNSATISFIABLE
    by construction: the last training label needs its forward window to have
    closed, so the cutoff can never be nearer than the horizon. A model
    retrained this morning flagged stale on arrival, and the same rule sat
    behind months of silently refused weekly promotions.

    The two axes fail for different real causes, both of which this project has
    experienced:

      * axis 1, TRAINING RECENCY (`trained_date` age) — catches "the retrain
        stopped running" (the per-ticker tournament frozen since April);
      * axis 2, CUTOFF LAG beyond the structural floor — catches "the inputs
        stopped advancing while retrains kept succeeding" (the fund-freshness
        serving-axis clip).

    One number cannot watch both, which is why the old rule missed one of them
    every time it was tuned to catch the other.
    """
    out: list[str] = []
    horizon = _declared_lookahead(health)

    if horizon is None:
        # FAIL CLOSED: no trustworthy horizon -> the old single-axis behaviour,
        # explicitly labelled so the record shows WHY it is being judged the
        # strict way rather than looking like a normal stale flag.
        if cutoff_lag_days > max_staleness_days:
            out.append(f"stale_{cutoff_lag_days}d_limit_{max_staleness_days}d")
            out.append("no_declared_lookahead_single_axis")
        return out

    floor = _trading_to_calendar_days(horizon)
    bound = floor + cutoff_lag_slack_days
    health["cutoff_lag_floor_days"] = floor
    health["cutoff_lag_bound_days"] = bound

    # axis 2
    if cutoff_lag_days > bound:
        out.append(
            f"cutoff_lag_{cutoff_lag_days}d_over_{bound}d"
            f"(floor_{floor}d+slack_{cutoff_lag_slack_days}d)")

    # axis 1
    trained_raw = health.get(TRAINED_DATE_FIELD)
    trained = _parse_cutoff_date(trained_raw)
    if trained_raw in (None, ""):
        health["trained_age_days"] = None
        out.append("missing_trained_date")
    elif trained is None:
        health["trained_age_days"] = None
        out.append("unparseable_trained_date")
    else:
        age = (run_date - trained).days
        health["trained_age_days"] = age
        if age < 0:
            out.append(f"trained_date_future_{age}d")
        elif age > max_staleness_days:
            out.append(f"trained_{age}d_limit_{max_staleness_days}d")
    return out


def finalize_shadow_health(
    health: dict[str, Any], *, run_date: datetime.date,
    max_staleness_days: int = DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    min_coverage_frac: float = DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
    cutoff_lag_slack_days: int = DEFAULT_CUTOFF_LAG_SLACK_DAYS,
) -> dict[str, Any]:
    """Derive ``state`` / ``status`` / ``actionable`` / ``reasons``.

    Expected-skip records (stamped via ``mark_expected_skip``) pass through
    unchanged. Otherwise:

    * not loaded → ``unresolved_artifact`` (ref didn't resolve) or
      ``load_failed`` (resolved but loader raised) — both FAULT.
    * loaded → FAULT (``degraded`` / ``not_scored``) if ANY of: stale/absent/
      future/unparseable train cutoff; absent required identity
      (``content_sha256`` / ``config_fingerprint``); config-pinned identity
      mismatch; low coverage; no usable scores. Else ``ok``.

    Pure / side-effect-free — directly unit-testable by all three consumers."""
    if health.get("state") in EXPECTED_SKIP_STATES:
        return _set_status(health, health["state"], health.get("reasons") or [health["state"]])

    if not health.get("loaded"):
        health["staleness_days"] = None
        if not health.get("artifact_resolved"):
            return _set_status(health, STATE_UNRESOLVED_ARTIFACT, ["artifact_unresolved"])
        return _set_status(health, STATE_LOAD_FAILED, ["load_failed"])

    reasons: list[str] = []

    # 1) Training-cutoff freshness (the stale-shadow class).
    cutoff_raw = health.get(TRAIN_CUTOFF_FIELD)
    cutoff = _parse_cutoff_date(cutoff_raw)
    if cutoff_raw in (None, ""):
        health["staleness_days"] = None
        reasons.append("missing_train_cutoff")
    elif cutoff is None:
        health["staleness_days"] = None
        reasons.append("unparseable_train_cutoff")
    else:
        staleness = (run_date - cutoff).days
        health["staleness_days"] = staleness
        if staleness < 0:
            reasons.append(f"train_cutoff_future_{staleness}d")
        else:
            reasons.extend(_freshness_reasons(
                health, run_date=run_date, cutoff_lag_days=staleness,
                max_staleness_days=max_staleness_days,
                cutoff_lag_slack_days=cutoff_lag_slack_days))

    # 1b) A frozen forward readout (certified artifact, fixed-length ledger)
    #     is stale BY DESIGN: suppress the freshness tokens only, keep them in
    #     the record, and leave every other fault class untouched.
    frozen = frozen_forward_readout_for(health, run_date=run_date)
    if frozen is not None:
        suppressed = [r for r in reasons if _is_frozen_suppressible(r)]
        reasons = [r for r in reasons if not _is_frozen_suppressible(r)]
        health["frozen_forward_readout"] = {
            "lane": frozen.lane,
            "content_sha256": frozen.content_sha256,
            "frozen_since": frozen.frozen_since.isoformat(),
            "until": frozen.until.isoformat(),
            "days_left": (frozen.until - run_date).days,
            "authority": frozen.authority,
            "freshness_suppressed": suppressed,
        }

    # 2) Required artifact IDENTITY (immutable content + provenance), plus any
    #    config-pinned expected identity (a swapped/wrong artifact → mismatch).
    reasons.extend(_identity_reasons(health))

    # 3) Coverage of the candidate cross-section.
    zero_scored = health.get("n_scored", 0) <= 0
    if zero_scored:
        reasons.append(health.get("skip_reason") or "no_scores")
    else:
        cov = health.get("coverage_frac")
        if cov is not None and cov < min_coverage_frac:
            reasons.append(f"low_coverage_{cov:.2f}_min_{min_coverage_frac:.2f}")

    if not reasons:
        return _set_status(health, STATE_OK, [])
    state = STATE_NOT_SCORED if zero_scored else STATE_DEGRADED
    return _set_status(health, state, reasons)


def _norm_digest(value: Any) -> str | None:
    """Normalize a content digest for comparison: strip an optional ``sha256:``
    prefix and lowercase, so a config pin written either way still matches."""
    if not value:
        return None
    return str(value).split(":", 1)[-1].strip().lower()


def _identity_reasons(health: dict[str, Any]) -> list[str]:
    """Reason tokens for absent required identity or a pinned-identity mismatch.

    Required identity = an immutable ``content_sha256`` (the artifact scoring
    actually used) AND a ``config_fingerprint`` (training provenance). A config
    pin (``expected_content_sha256`` / ``expected_config_fingerprint``) that
    disagrees with the observed identity is a MISMATCH — the file at the path
    is not the artifact the config expects."""
    out: list[str] = []
    content = health.get("content_sha256")
    fp = health.get(CONFIG_FINGERPRINT_FIELD)
    if not content:
        out.append("missing_content_sha256")
    if not fp:
        out.append("missing_config_fingerprint")
    exp_content = health.get("expected_content_sha256")
    if exp_content and content and _norm_digest(exp_content) != _norm_digest(content):
        out.append("content_sha256_mismatch")
    exp_fp = health.get("expected_config_fingerprint")
    if exp_fp and fp and str(exp_fp).strip() != str(fp).strip():
        out.append("config_fingerprint_mismatch")
    return out


def _is_frozen_suppressible(reason: Any) -> bool:
    text = str(reason or "")
    return any(p.match(text) for p in _FROZEN_SUPPRESSIBLE)


def frozen_forward_readout_for(
    health: dict[str, Any], *, run_date: datetime.date,
) -> FrozenForwardReadout | None:
    """The registered frozen readout this record is entitled to, or None.

    Entitlement needs ALL of: the lane name matches; the OBSERVED
    ``content_sha256`` equals the certified digest (the bytes that scored);
    the config-pinned ``expected_content_sha256`` equals it too (the served
    config still declares this exact artifact); and ``run_date`` is inside
    ``[frozen_since, until]``. Anything missing → None → the standing rule.
    Digests compare on the shorter of the two normalized forms so a 16-hex
    config pin and a full digest agree, exactly as ``_identity_reasons``
    compares them.
    """
    observed = _norm_digest(health.get("content_sha256"))
    pinned = _norm_digest(health.get("expected_content_sha256"))
    if not observed or not pinned:
        return None
    for entry in FROZEN_FORWARD_READOUTS:
        if not entry.matches_lane(health.get("shadow_name")):
            continue
        cert = _norm_digest(entry.content_sha256)
        if not cert:
            continue
        if not _digest_prefix_equal(observed, cert):
            continue
        if not _digest_prefix_equal(pinned, cert):
            continue
        if run_date < entry.frozen_since or run_date > entry.until:
            continue
        return entry
    return None


def _digest_prefix_equal(a: str, b: str) -> bool:
    """Equal on the shorter length, minimum 16 hex (the config-pin form)."""
    n = min(len(a), len(b))
    if n < 16:
        return False
    return a[:n] == b[:n]


# ── Sink resolution + append ───────────────────────────────────────────────────

def shadow_health_cfg(config: dict) -> dict:
    raw = (config or {}).get("shadow_health")
    return raw if isinstance(raw, dict) else {}


def shadow_health_log_path(config: dict) -> Path:
    """Resolve the append-only JSONL sink for shadow-scorer health records.

    Default: ``<config["_strategy_dir"]>/logs/shadow_scorer_health.jsonl``
    (mirrors the AdmissionShadowLoggerTask sink convention). Overridable via
    ``config["shadow_health"]["path"]``. Falls back to ``./logs/...`` when no
    strategy_dir is set (sim/test)."""
    override = shadow_health_cfg(config).get("path")
    if override:
        return Path(str(override))
    strategy_dir = (config or {}).get("_strategy_dir")
    base = Path(str(strategy_dir)) if strategy_dir else Path(".")
    return base / DEFAULT_SHADOW_HEALTH_RELPATH


def shadow_health_sink_defined(config: dict) -> bool:
    """True when a health sink location is explicitly configured — either a
    ``shadow_health.path`` override or a ``_strategy_dir``. When neither is set
    the writer skips rather than scatter the file in a bare cwd."""
    return bool(shadow_health_cfg(config).get("path")) or bool(
        (config or {}).get("_strategy_dir"))


def append_shadow_health(path: str | Path, record: dict[str, Any]) -> None:
    """Append one health record as a JSON line to ``path`` (creates dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


__all__ = [
    "SHADOW_HEALTH_SCHEMA",
    "DEFAULT_SHADOW_HEALTH_RELPATH",
    "DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS",
    "DEFAULT_CUTOFF_LAG_SLACK_DAYS",
    "TRAINED_DATE_FIELD",
    "LOOKAHEAD_FIELD",
    "DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC",
    "STATUS_OK",
    "STATUS_EXPECTED_SKIP",
    "STATUS_FAULT",
    "STATE_DISABLED",
    "STATE_NO_SHADOW_MODELS",
    "TASK_LEVEL_SHADOW_NAME",
    "STATE_NO_CANDIDATES",
    "STATE_NOT_YET_PUBLISHED",
    "ShadowNotYetPublished",
    "STATE_OK",
    "STATE_DEGRADED",
    "STATE_NOT_SCORED",
    "STATE_UNRESOLVED_ARTIFACT",
    "STATE_LOAD_FAILED",
    "EXPECTED_SKIP_STATES",
    "FAULT_STATES",
    "TRAIN_CUTOFF_FIELD",
    "CONFIG_FINGERPRINT_FIELD",
    "CONTENT_SHA256_PREFIX",
    "STRATEGY_CONFIG_PATH_KEY",
    "ArtifactIdentity",
    "content_digest",
    "task_config_identity",
    "resolve_artifact_identity",
    "new_shadow_health",
    "mark_expected_skip",
    "finalize_shadow_health",
    "shadow_health_cfg",
    "shadow_health_log_path",
    "shadow_health_sink_defined",
    "append_shadow_health",
]
