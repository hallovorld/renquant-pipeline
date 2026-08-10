"""Governed operator override for admitting buys on a WF-FAIL artifact.

The walk-forward gate (P-WF-GATE) stamps ``metadata.wf_gate_metadata.passed``.
``passed is False`` is the gate's STRONGEST negative verdict: the candidate was
evaluated and rejected (e.g. ``benchmark_ok=False``, ΔSharpe negative vs SPY).
P-WF-GATE HARD-fails a full/buy run on it. That refusal is correct by default.

This module defines the ONLY sanctioned exception at the ``passed is False``
branch — an explicit, expiring, scorer-content-bound, WF-reason-acknowledged
operator authorization ("I accept the risk, buy anyway") carried in strategy
config::

    "wf_gate": {
      "wf_fail_buy_admission": {
        "authorized": true,
        "operator": "renhao",
        "authorized_at": "2026-08-10",
        "expires": "2026-08-24",
        "scorer_model_content_sha256": "sha256:656b70be…",
        "wf_reason_acknowledged": "FAIL: absolute_ok=True, benchmark_ok=False, "
                                  "regime_ok=False; mean Sharpe +0.602, …",
        "reason": "…"
      }
    }

DISTINCT FROM ``diagnostic_only_override``. That module governs the WEAKER
``passed=True + diagnostic_only=True`` stamp (research evidence flagged
non-tradeable) under the ``diagnostic_only_buy_admission`` key. A hard
``passed=False`` is a STRONGER negative and REQUIRES this separate, more
stringent authorization under the DIFFERENT ``wf_fail_buy_admission`` key. The
two paths never overlap: a ``diagnostic_only_buy_admission`` authorization can
NEVER admit a ``passed=False`` artifact (P-WF-GATE only consults the
diagnostic-only override on the ``passed=True`` branch), and this override is
consulted ONLY on the ``passed=False`` branch.

Governance properties (all load-bearing — reviewers: treat any relaxation as
a security regression; mirror ``diagnostic_only_override`` and its tests):

* **Fail-closed.** No block, a malformed block, an unparseable date, a
  missing/mismatched scorer hash, an unavailable hash, or a wf_reason that does
  not byte-match all leave the default refusal in place. A defect is logged as
  a WARNING naming the field — a malformed authorization must never widen
  access.
* **Expiring.** ``expires`` is required; the day AFTER ``expires`` the refusal
  returns automatically (comparison by date, UTC when the caller supplies no
  trading date). No unbounded overrides.
* **Scorer-content-bound.** The authorization names the schema-v1 content hash
  (``renquant_common.model_fingerprint.model_content_sha256``) of the ONE
  scorer it covers. A re-promoted / retrained / substituted artifact does not
  inherit it.
* **WF-reason-acknowledged (the extra stringency vs diagnostic_only).**
  ``wf_reason_acknowledged`` MUST byte-equal the artifact's actual
  ``wf_gate_metadata.wf_reason`` string, so the operator provably saw the
  SPECIFIC failure they are overriding. A stale authorization written for a
  different failure (a re-run that flips ``benchmark_ok`` / shifts ΔSharpe /
  changes the benchmark-lag regimes) does not admit — the wf_reason string
  moved and the authorization no longer matches.
* **Audited.** The full authorization record plus the computed scorer hash and
  the acknowledged wf_reason is returned as ``provenance`` and must be attached
  to the admitting check's details / run-bundle surface by callers.

The authorization RECORD lives in strategy config (renquant-strategy-104),
outside the model-relevant config-fingerprint projection
(``renquant_common.config_consistency._model_relevant_fields``), so adding or
expiring an authorization never invalidates artifact config-consistency stamps.
``tests/test_wf_fail_override.py`` pins that property.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field

log = logging.getLogger("wf_fail_override")

_REQUIRED_STR_FIELDS = ("operator", "authorized_at", "expires",
                        "scorer_model_content_sha256", "wf_reason_acknowledged",
                        "reason")


@dataclass(frozen=True)
class WfFailOverrideVerdict:
    """Outcome of validating a WF-FAIL buy-admission authorization."""

    authorized: bool
    reason: str
    provenance: dict = field(default_factory=dict)


def _normalize_sha(value: str | None) -> str:
    return str(value or "").strip().lower().removeprefix("sha256:")


def _parse_iso_date(value: object) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def scorer_content_sha_from_payload(payload: dict | None) -> str | None:
    """Schema-v1 content hash of the active scorer payload, or None (fail-closed).

    A None return (no payload, or the hash implementation unavailable) makes
    ``evaluate_wf_fail_override`` refuse — an override that cannot verify which
    scorer it covers is not an override. A missing/empty payload returns None
    quietly (the common no-authorization hard-fail path calls this eagerly);
    when a ``wf_fail_buy_admission`` block IS present the validator surfaces the
    specific ``scorer_hash_unavailable`` verdict. A genuine hash failure on a
    real payload is logged as a WARNING.
    """
    if not isinstance(payload, dict) or not payload:
        return None
    try:
        from renquant_common.model_fingerprint import (  # noqa: PLC0415
            model_content_sha256,
        )
        return model_content_sha256(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "wf_fail_buy_admission: v1 content hash of the active scorer "
            "unavailable (%s) — cannot verify scorer binding, refusal stands",
            exc,
        )
        return None


def evaluate_wf_fail_override(
    wf: dict | None,
    config: dict | None,
    *,
    scorer_content_sha: str | None,
    now: _dt.date | None = None,
) -> WfFailOverrideVerdict:
    """Validate the operator authorization against the ACTIVE WF-FAIL artifact.

    ``wf`` is the artifact's ``wf_gate_metadata`` dict (its ``wf_reason`` is the
    byte-equality target). ``scorer_content_sha`` is the schema-v1 content hash
    of the active scorer the caller already resolved
    (``scorer_content_sha_from_payload`` for the preflight path;
    ``metadata['model_content_fingerprint_v1_recompute']`` for the scoring
    path). Exactly the authorization-bound scorer AND the acknowledged failure
    reason must match; anything else fails closed.
    """
    block = ((config or {}).get("wf_gate") or {}).get("wf_fail_buy_admission")
    if block is None:
        return WfFailOverrideVerdict(False, "absent")
    if not isinstance(block, dict):
        log.warning(
            "wf_fail_buy_admission present but not a dict (%s) — override "
            "ignored, refusal stands", type(block).__name__,
        )
        return WfFailOverrideVerdict(False, "malformed:not_a_dict")

    if block.get("authorized") is not True:
        log.warning(
            "wf_fail_buy_admission.authorized is %r (must be exactly true) — "
            "override ignored, refusal stands", block.get("authorized"),
        )
        return WfFailOverrideVerdict(False, "malformed:authorized")
    for key in _REQUIRED_STR_FIELDS:
        value = block.get(key)
        if not isinstance(value, str) or not value.strip():
            log.warning(
                "wf_fail_buy_admission.%s missing/empty — override ignored, "
                "refusal stands", key,
            )
            return WfFailOverrideVerdict(False, f"malformed:{key}")
    if _parse_iso_date(block["authorized_at"]) is None:
        log.warning(
            "wf_fail_buy_admission.authorized_at %r is not an ISO date — "
            "override ignored, refusal stands", block["authorized_at"],
        )
        return WfFailOverrideVerdict(False, "malformed:authorized_at")
    expires = _parse_iso_date(block["expires"])
    if expires is None:
        log.warning(
            "wf_fail_buy_admission.expires %r is not an ISO date — override "
            "ignored, refusal stands", block["expires"],
        )
        return WfFailOverrideVerdict(False, "malformed:expires")

    effective_today = now or _dt.datetime.now(_dt.timezone.utc).date()
    if expires < effective_today:
        log.warning(
            "wf_fail_buy_admission expired %s (today=%s) — refusal stands; a "
            "new authorization requires a fresh config review",
            expires, effective_today,
        )
        return WfFailOverrideVerdict(
            False, "expired",
            {"expires": str(expires), "today": str(effective_today)},
        )

    if scorer_content_sha is None:
        # The helper already logged the specific cause (no payload / no hash).
        return WfFailOverrideVerdict(False, "scorer_hash_unavailable")
    authorized_sha = _normalize_sha(block["scorer_model_content_sha256"])
    if not authorized_sha or authorized_sha != _normalize_sha(scorer_content_sha):
        log.warning(
            "wf_fail_buy_admission bound to scorer %s but active scorer is %s "
            "— override does not transfer, refusal stands",
            block["scorer_model_content_sha256"], scorer_content_sha,
        )
        return WfFailOverrideVerdict(
            False, "scorer_mismatch",
            {"authorized": block["scorer_model_content_sha256"],
             "active": scorer_content_sha},
        )

    # Extra stringency vs diagnostic_only: the operator must have acknowledged
    # the EXACT failure they are overriding. A byte-mismatch (or a missing
    # wf_reason on the artifact) means the authorization was written for a
    # different verdict — refuse.
    actual_wf_reason = wf.get("wf_reason") if isinstance(wf, dict) else None
    if not isinstance(actual_wf_reason, str) or not actual_wf_reason.strip():
        log.warning(
            "wf_fail_buy_admission: artifact carries no wf_reason string to "
            "acknowledge (%r) — cannot verify the operator saw this specific "
            "failure, refusal stands", actual_wf_reason,
        )
        return WfFailOverrideVerdict(
            False, "wf_reason_unavailable",
            {"acknowledged": block["wf_reason_acknowledged"],
             "actual": actual_wf_reason},
        )
    if block["wf_reason_acknowledged"] != actual_wf_reason:
        log.warning(
            "wf_fail_buy_admission acknowledged wf_reason %r but the artifact's "
            "wf_reason is %r — the failure moved, the authorization does not "
            "transfer, refusal stands",
            block["wf_reason_acknowledged"], actual_wf_reason,
        )
        return WfFailOverrideVerdict(
            False, "wf_reason_mismatch",
            {"acknowledged": block["wf_reason_acknowledged"],
             "actual": actual_wf_reason},
        )

    provenance = {
        "operator": block["operator"],
        "authorized_at": block["authorized_at"],
        "expires": block["expires"],
        "reason": block["reason"],
        "scorer_model_content_sha256": block["scorer_model_content_sha256"],
        "active_scorer_content_sha256": scorer_content_sha,
        "wf_reason_acknowledged": actual_wf_reason,
    }
    return WfFailOverrideVerdict(True, "authorized", provenance)
