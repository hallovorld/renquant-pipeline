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
STATE_NO_CANDIDATES = "no_candidates"        # nothing to score this run (per-model)
STATE_OK = "ok"                              # loaded + fresh + provenanced + covered
STATE_DEGRADED = "degraded"                  # loaded but stale/low-cov/identity issue
STATE_NOT_SCORED = "not_scored"              # loaded but produced no usable scores
STATE_UNRESOLVED_ARTIFACT = "unresolved_artifact"  # ref did not resolve (../.. class)
STATE_LOAD_FAILED = "load_failed"            # resolved but scorer_loader raised

EXPECTED_SKIP_STATES = frozenset({
    STATE_DISABLED, STATE_NO_SHADOW_MODELS, STATE_NO_CANDIDATES,
})
FAULT_STATES = frozenset({
    STATE_DEGRADED, STATE_NOT_SCORED, STATE_UNRESOLVED_ARTIFACT, STATE_LOAD_FAILED,
})

# Provenance / identity metadata field names read off the loaded scorer.
TRAIN_CUTOFF_FIELD = "effective_train_cutoff_date"
CONFIG_FINGERPRINT_FIELD = "config_fingerprint"

CONTENT_SHA256_PREFIX = "sha256:"

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
        "n_scored": 0,
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


def finalize_shadow_health(
    health: dict[str, Any], *, run_date: datetime.date,
    max_staleness_days: int = DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    min_coverage_frac: float = DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
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
        elif staleness > max_staleness_days:
            reasons.append(f"stale_{staleness}d_limit_{max_staleness_days}d")

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
    "DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC",
    "STATUS_OK",
    "STATUS_EXPECTED_SKIP",
    "STATUS_FAULT",
    "STATE_DISABLED",
    "STATE_NO_SHADOW_MODELS",
    "STATE_NO_CANDIDATES",
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
    "ArtifactIdentity",
    "content_digest",
    "resolve_artifact_identity",
    "new_shadow_health",
    "mark_expected_skip",
    "finalize_shadow_health",
    "shadow_health_cfg",
    "shadow_health_log_path",
    "shadow_health_sink_defined",
    "append_shadow_health",
]
