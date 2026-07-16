"""Governed operator override for diagnostic-only buy admission.

A scorer whose walk-forward evidence is stamped ``diagnostic_only=True`` is
refused buy admission at two independent enforcement points:

  1. Preflight ``P-WF-GATE`` (``preflight_pipeline.tasks.gate``) — aborts a
     full/buy run before any broker decision.
  2. Scoring-path ``_diagnostic_only_admission``
     (``panel_pipeline.job_panel_scoring``) — clears buy candidates even if
     preflight was bypassed.

Both refusals are correct by default: a diagnostic-only WF result is research
evidence, not a trading authorization. This module defines the ONLY sanctioned
exception — an explicit, expiring, scorer-content-bound operator authorization
carried in strategy config::

    "wf_gate": {
      "diagnostic_only_buy_admission": {
        "authorized": true,
        "operator": "renhao",
        "authorized_at": "2026-07-16",
        "expires": "2026-08-15",
        "scorer_model_content_sha256": "sha256:656b70be…",
        "reason": "…"
      }
    }

Governance properties (all load-bearing — reviewers: treat any relaxation as
a security regression):

* **Fail-closed.** No block, a malformed block, an unparseable date, a
  missing/mismatched scorer hash, or an unavailable hash implementation all
  leave the default refusal in place. A defect is logged as a WARNING naming
  the field — a malformed authorization must never widen access.
* **Expiring.** ``expires`` is required; the day AFTER ``expires`` the
  refusal returns automatically (comparison is by date, UTC when the caller
  supplies no trading date). No unbounded overrides.
* **Scorer-content-bound.** The authorization names the schema-v1 content
  hash (``renquant_common.model_fingerprint.model_content_sha256``) of the
  ONE scorer it covers. A re-promoted / retrained / substituted artifact
  does not inherit the authorization.
* **Audited.** The full authorization record plus the computed scorer hash
  is returned as ``provenance`` and must be attached to the admitting
  check's details / run-bundle surface by callers.

The authorization RECORD lives in strategy config (renquant-strategy-104),
outside the model-relevant config-fingerprint projection
(``renquant_common.config_consistency._model_relevant_fields`` hashes only
watchlist / panel_ltr / sector maps / benchmark / resolution flags), so
adding or expiring an authorization never invalidates artifact
config-consistency stamps. ``tests/test_diagnostic_only_override.py`` pins
that property.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("diagnostic_only_override")

_REQUIRED_STR_FIELDS = ("operator", "authorized_at", "expires",
                        "scorer_model_content_sha256", "reason")


@dataclass(frozen=True)
class OverrideVerdict:
    """Outcome of validating a diagnostic-only buy-admission authorization."""

    authorized: bool
    reason: str
    provenance: dict = field(default_factory=dict)


def _normalize_sha(value: str | None) -> str:
    return str(value or "").strip().lower().removeprefix("sha256:")


def _parse_iso_date(value: Any) -> _dt.date | None:
    try:
        return _dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def evaluate_diagnostic_only_override(
    config: dict | None,
    *,
    scorer_payload: dict | None = None,
    scorer_v1_fingerprint: str | None = None,
    today: _dt.date | None = None,
) -> OverrideVerdict:
    """Validate the operator authorization against the ACTIVE scorer.

    ``scorer_payload`` (preferred, preflight path): the loaded panel artifact
    payload; its schema-v1 content hash is computed here via renquant-common.
    ``scorer_v1_fingerprint`` (scoring path): a v1 hash the caller already
    holds (e.g. ``PanelScorer.metadata['model_content_fingerprint_v1_recompute']``).
    Exactly the authorization-bound scorer must be active; anything else
    fails closed.
    """
    block = ((config or {}).get("wf_gate") or {}).get(
        "diagnostic_only_buy_admission"
    )
    if block is None:
        return OverrideVerdict(False, "absent")
    if not isinstance(block, dict):
        log.warning(
            "diagnostic_only_buy_admission present but not a dict (%s) — "
            "override ignored, refusal stands", type(block).__name__,
        )
        return OverrideVerdict(False, "malformed:not_a_dict")

    if block.get("authorized") is not True:
        log.warning(
            "diagnostic_only_buy_admission.authorized is %r (must be exactly "
            "true) — override ignored, refusal stands", block.get("authorized"),
        )
        return OverrideVerdict(False, "malformed:authorized")
    for key in _REQUIRED_STR_FIELDS:
        value = block.get(key)
        if not isinstance(value, str) or not value.strip():
            log.warning(
                "diagnostic_only_buy_admission.%s missing/empty — override "
                "ignored, refusal stands", key,
            )
            return OverrideVerdict(False, f"malformed:{key}")
    if _parse_iso_date(block["authorized_at"]) is None:
        log.warning(
            "diagnostic_only_buy_admission.authorized_at %r is not an ISO "
            "date — override ignored, refusal stands", block["authorized_at"],
        )
        return OverrideVerdict(False, "malformed:authorized_at")
    expires = _parse_iso_date(block["expires"])
    if expires is None:
        log.warning(
            "diagnostic_only_buy_admission.expires %r is not an ISO date — "
            "override ignored, refusal stands", block["expires"],
        )
        return OverrideVerdict(False, "malformed:expires")

    effective_today = today or _dt.datetime.now(_dt.timezone.utc).date()
    if expires < effective_today:
        log.warning(
            "diagnostic_only_buy_admission expired %s (today=%s) — refusal "
            "stands; a new authorization requires a fresh config review",
            expires, effective_today,
        )
        return OverrideVerdict(
            False, "expired",
            {"expires": str(expires), "today": str(effective_today)},
        )

    active_v1 = _resolve_active_scorer_v1(scorer_payload, scorer_v1_fingerprint)
    if active_v1 is None:
        return OverrideVerdict(False, "scorer_hash_unavailable")
    authorized_sha = _normalize_sha(block["scorer_model_content_sha256"])
    if not authorized_sha or authorized_sha != _normalize_sha(active_v1):
        log.warning(
            "diagnostic_only_buy_admission bound to scorer %s but active "
            "scorer is %s — override does not transfer, refusal stands",
            block["scorer_model_content_sha256"], active_v1,
        )
        return OverrideVerdict(
            False, "scorer_mismatch",
            {"authorized": block["scorer_model_content_sha256"],
             "active": active_v1},
        )

    provenance = {
        "operator": block["operator"],
        "authorized_at": block["authorized_at"],
        "expires": block["expires"],
        "reason": block["reason"],
        "scorer_model_content_sha256": block["scorer_model_content_sha256"],
        "active_scorer_v1": active_v1,
    }
    return OverrideVerdict(True, "authorized", provenance)


def _resolve_active_scorer_v1(
    scorer_payload: dict | None,
    scorer_v1_fingerprint: str | None,
) -> str | None:
    if scorer_v1_fingerprint:
        return str(scorer_v1_fingerprint)
    if not isinstance(scorer_payload, dict) or not scorer_payload:
        log.warning(
            "diagnostic_only_buy_admission: no active-scorer payload or v1 "
            "fingerprint supplied — cannot verify scorer binding, refusal "
            "stands",
        )
        return None
    try:
        from renquant_common.model_fingerprint import (  # noqa: PLC0415
            model_content_sha256,
        )
        return model_content_sha256(scorer_payload)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "diagnostic_only_buy_admission: v1 content hash of the active "
            "scorer unavailable (%s) — cannot verify scorer binding, refusal "
            "stands", exc,
        )
        return None
