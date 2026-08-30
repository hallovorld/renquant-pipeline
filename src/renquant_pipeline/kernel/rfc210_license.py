"""RFC #210 freshness-fallback serving license for P-WF-GATE.

2026-08-04 incident: the first RFC #210 governance promotion (operator P0,
renquant-backtesting#101/#102) stamped the ACTIVE panel artifact with
``metadata.promotion_basis = "freshness_fallback_rfc210"`` and — by design —
``wf_gate_metadata.passed = False`` (the fallback's whole point is that the
gate rejected the candidate but the freshness governance already decided the
serving question). P-WF-GATE read ``passed`` alone, hard-failed the full run,
and the book went sell-only on the new model's first day.

This module is the license both P-WF-GATE twins consult before hard-failing a
``passed=False`` artifact: a governance-served artifact is admitted while it
stays fresh, and ONLY then. Everything else about the check is unchanged —
wrong basis string, missing/unparseable trained date, or an aged-out artifact
all fall through to the existing hard fail. Fail closed toward refusal.

The age bar defaults to 28 days — RFC #210's own serving SLA (model freshness
governance: no model >28d). ``strategy_config.wf_gate.rfc210_max_served_age_days``
overrides it; the deployed ``model_staleness_days=60`` split is tracked in
renquant-orchestrator#745 and is deliberately NOT consulted here — one policy
number per license, from the policy that created the license.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

PROMOTION_BASIS = "freshness_fallback_rfc210"
DEFAULT_MAX_SERVED_AGE_DAYS = 28


@dataclass(frozen=True)
class Rfc210License:
    served: bool
    reason: str
    provenance: dict = field(default_factory=dict)


def _promotion_basis(payload: dict) -> object:
    meta = payload.get("metadata")
    if isinstance(meta, dict) and "promotion_basis" in meta:
        return meta.get("promotion_basis")
    return payload.get("promotion_basis")


def _trained_date(payload: dict) -> object:
    if "trained_date" in payload:
        return payload.get("trained_date")
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        return meta.get("trained_date")
    return None


def evaluate_freshness_fallback_license(
    payload: object,
    *,
    config: dict | None = None,
    today: dt.date | None = None,
) -> Rfc210License:
    """Decide whether a gate-failed artifact is governance-served under RFC #210.

    Returns ``served=True`` ONLY when every condition proves out:
      1. ``promotion_basis`` is exactly ``"freshness_fallback_rfc210"``
         (metadata first, top-level fallback);
      2. ``trained_date`` is a parseable ISO date (top-level first, metadata
         fallback);
      3. its age in days, relative to ``today``, is 0..max_served_age_days
         (a future trained_date is refused — that is corrupt evidence, not
         freshness).
    Anything else → ``served=False`` with the specific reason.
    """
    if not isinstance(payload, dict):
        return Rfc210License(False, "artifact payload is not an object")
    basis = _promotion_basis(payload)
    if basis != PROMOTION_BASIS:
        return Rfc210License(False, f"promotion_basis is {basis!r}, not {PROMOTION_BASIS!r}")
    raw = _trained_date(payload)
    if not isinstance(raw, str) or not raw.strip():
        return Rfc210License(False, f"trained_date is {raw!r}")
    try:
        trained = dt.date.fromisoformat(raw.strip())
    except ValueError:
        return Rfc210License(False, f"trained_date {raw!r} is not an ISO date")
    today = today if today is not None else dt.date.today()
    age = (today - trained).days
    if age < 0:
        return Rfc210License(False, f"trained_date {raw} is in the future ({-age}d)")
    max_age = DEFAULT_MAX_SERVED_AGE_DAYS
    cfg = (config or {}).get("wf_gate") if isinstance(config, dict) else None
    if isinstance(cfg, dict):
        override = cfg.get("rfc210_max_served_age_days")
        if isinstance(override, int) and not isinstance(override, bool) and override > 0:
            max_age = override
    if age > max_age:
        return Rfc210License(
            False,
            f"governance-served artifact aged out: trained {raw}, {age}d old "
            f"> {max_age}d RFC#210 serving SLA",
        )
    return Rfc210License(
        True,
        f"governance-served under RFC#210: trained {raw}, {age}d old "
        f"<= {max_age}d serving SLA",
        provenance={
            "promotion_basis": PROMOTION_BASIS,
            "trained_date": raw,
            "age_days": age,
            "max_served_age_days": max_age,
        },
    )


def genuine_ic_from_payload(payload: object) -> float | None:
    """The genuine IC the fallback promotion recorded for this artifact.

    ``metadata.fallback_genuine_ic`` is what the RFC #210 promoter stamps;
    ``wf_gate_metadata.sanity_placebo_genuine_ic`` is the gate's own copy.
    ``None`` when neither is a finite number — the caller prints ``n/a``,
    never a made-up figure.
    """
    if not isinstance(payload, dict):
        return None
    meta = payload.get("metadata")
    candidates: list[object] = []
    if isinstance(meta, dict):
        candidates.append(meta.get("fallback_genuine_ic"))
        wf = meta.get("wf_gate_metadata")
        if isinstance(wf, dict):
            candidates.append(wf.get("sanity_placebo_genuine_ic"))
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            f = float(value)
            if f == f and f not in (float("inf"), float("-inf")):
                return f
    return None


def licensed_check_message(license: Rfc210License, wf: dict, payload: object) -> str:
    """The P-WF-GATE ✓ text while a gate-FAILED artifact is served under RFC #210.

    2026-08-30: the served artifact (trained 2026-08-02, passed=false,
    genuine_ic=+0.0029) logged ``✓ P-WF-GATE ... governance-served ... buys
    admitted`` — a reader saw a pass. The licensed state now leads the line,
    with the fact that the gate FAILED, the genuine IC and the age-vs-SLA the
    license is about, so the log never prints a bare ✓ for a licensed gate.
    """
    prov = license.provenance
    gi = genuine_ic_from_payload(payload)
    gi_s = f"{gi:+.4f}" if gi is not None else "n/a"
    return (
        f"LICENSED: WF gate FAILED, genuine_ic={gi_s}, served age "
        f"{prov.get('age_days')}d ≤ {prov.get('max_served_age_days')} "
        f"(promotion_basis={prov.get('promotion_basis')}, trained "
        f"{prov.get('trained_date')}; wf_sharpe_mean={wf.get('wf_3cut_sharpe_mean')}, "
        f"reason={wf.get('wf_reason')}) — buys admitted ONLY while the RFC#210 "
        "freshness license holds; this is not a WF pass."
    )
