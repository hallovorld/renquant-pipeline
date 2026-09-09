"""A reviewed extension may carry ONE artifact's A4-T1 window — nothing else.

2026-09-07 the stamped window closed exactly as designed and P-REGIME-IC went
back to HARD, so the 2026-09-08 daily aborted to sell-only. There is no other
servable artifact: the previous model is 37d old against a 28d SLA and every
candidate since 09-01 has zero eligible regimes, so without an extension the
book cannot buy at all. This registry is how a second window is granted: in
reviewed code, bound to the run id AND the promoted artifact's digest, with
its own hard end date — never by rewriting the served artifact, whose stamp
the orchestrator's consumption ledger binds.

Every test freezes `today`; none touches the live tree.
"""
from __future__ import annotations

import datetime as dt

import pytest

from renquant_pipeline.kernel import rfc210_license as lic
from renquant_pipeline.kernel.rfc210_license import (
    A4T1_WINDOW_EXTENSIONS,
    A4T1WindowExtension,
    a4t1_window_extension,
    evaluate_a4t1_regime_evidence_license as ev,
)

RUN_ID = "20260831T141820Z"
DIGEST = "760912ec122fa6e02628077df8b35e58145209ea3b6b395bd670d8ead9e4af1e"
RECEIPT = "2cd9d27b0b96835119827de0760213a0539e71ac7574c213a74f68a5cc772d6e"
STAMPED = "2026-09-07"


def _payload(*, trained_days_old: int = 4, today: dt.date, digest: str | None = DIGEST,
             run_id: str = RUN_ID, stamped: str = STAMPED) -> dict:
    meta = {
        "promotion_basis": "freshness_fallback_rfc210",
        "fallback_genuine_ic": 0.001553838965806277,
        "fallback_quality_floor": 0.001,
        "fallback_a4t1_override": True,
        "fallback_a4t1_expiry": stamped,
        "fallback_a4t1_candidate_run_id": run_id,
        "fallback_a4t1_candidate_authority":
            "renquant-orchestrator:ops/governance/a4t1/20260831T141820Z.authorization.json",
        "fallback_a4t1_consumption_proof": {
            "schema": "a4t1_consumption_proof.v1", "receipt_id": RECEIPT,
            "consumed_by": "renquant-orchestrator"},
        # the real stamped block of the promoted candidate (zero-trade WF)
        "wf_gate_metadata": {
            "passed": False, "diagnostic_only": False,
            "wf_reason": "FAIL: zero trades across all WF cuts",
            "sanity_placebo_genuine_ic": 0.001553838965806277,
            "sanity_regime_ic": {"passed": False,
                                 "reason": "regime sanity IC failed: BULL_CALM,BULL_VOLATILE,CHOPPY"},
            "trade_contract": {"passed": False, "reason": "no round-trip ledgers found"},
            "trade_monotonicity": {"passed": False, "reason": "no round-trip ledgers found"},
            "alpha_economics": {"passed": False, "reason": "no round-trip ledgers found"},
        },
    }
    if digest is not None:
        meta["fallback_a4t1_candidate_digest"] = digest
    return {"kind": "panel_ltr_xgboost",
            "trained_date": (today - dt.timedelta(days=trained_days_old)).isoformat(),
            "feature_cols": ["f1"], "metadata": meta}


def _config() -> dict:
    return {"ranking": {"panel_scoring": {
        "enabled": True, "kind": "xgb",
        "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
        "regime_admission": {"enabled": False}}},
        "wf_gate": {"sanity_regime_ic_required": False}}


# ── the shipped entry ─────────────────────────────────────────────────────

def test_registry_holds_exactly_the_one_promoted_artifact():
    assert len(A4T1_WINDOW_EXTENSIONS) == 1
    ext = A4T1_WINDOW_EXTENSIONS[0]
    assert isinstance(ext, A4T1WindowExtension)
    assert ext.run_id == RUN_ID and ext.run_id in lic.A4T1_LICENSED_RUN_IDS
    assert ext.artifact_digest == DIGEST
    assert ext.until == dt.date(2026, 9, 28)
    assert "row 2h" in ext.authority and ext.reason.strip()


def test_the_2026_09_08_artifact_is_served_again_and_the_text_says_extended():
    today = dt.date(2026, 9, 8)
    v = ev(_payload(today=today), today=today)
    assert v.served, v.reason
    assert "EXTENDED by" in v.reason and "row 2h" in v.reason
    assert v.provenance["a4t1_window_extended"] is True
    assert v.provenance["a4t1_stamped_expiry"] == STAMPED       # never rewritten
    assert v.provenance["a4t1_expiry"] == "2026-09-28"
    assert v.provenance["a4t1_days_left"] == (dt.date(2026, 9, 28) - today).days
    assert v.provenance["a4t1_receipt_id"] == RECEIPT


def test_inside_the_stamped_window_nothing_changes():
    today = dt.date(2026, 9, 4)
    v = ev(_payload(today=today), today=today)
    assert v.served and "EXTENDED" not in v.reason
    assert "a4t1_window_extended" not in v.provenance
    assert v.provenance["a4t1_expiry"] == STAMPED


def test_the_extension_closes_by_itself():
    for day, served in ((dt.date(2026, 9, 28), True), (dt.date(2026, 9, 29), False)):
        v = ev(_payload(today=day, trained_days_old=4), today=day)
        assert v.served is served, day
        if not served:
            assert "window closed" in v.reason


def test_no_extension_may_outlive_the_artifact_it_extends():
    """A window only decides whether the regime-evidence exception applies; the
    artifact must ALSO hold the ordinary RFC#210 license, whose age bar is
    `DEFAULT_MAX_SERVED_AGE_DAYS`. The first version of this entry ran to
    2026-10-16 while the artifact (trained 2026-08-31) stops being servable
    after 2026-09-28 — the last 18 days were inert, and a ledger row that grants
    a window the age bar overrides is authority the system cannot honour.

    This is the class guard: every registered extension must end on or before
    its artifact's age-bar ceiling.
    """
    trained = dt.date(2026, 8, 31)          # the one promoted artifact
    ceiling = trained + dt.timedelta(days=lic.DEFAULT_MAX_SERVED_AGE_DAYS)
    assert ceiling == dt.date(2026, 9, 28)
    for ext in A4T1_WINDOW_EXTENSIONS:
        assert ext.until <= ceiling, (ext.run_id, ext.until, ceiling)

    # and the ceiling is real, not asserted: one day past it the license refuses
    # on AGE, with the window still nominally open.
    day = ceiling + dt.timedelta(days=1)
    v = ev(_payload(today=day, trained_days_old=(day - trained).days), today=day)
    assert v.served is False
    assert "aged out" in v.reason, v.reason


def test_extension_binds_the_artifact_digest():
    today = dt.date(2026, 9, 8)
    for digest in (None, "", "0" * 64, DIGEST[:32]):
        v = ev(_payload(today=today, digest=digest), today=today)
        assert not v.served and "window closed" in v.reason, digest
    upper = ev(_payload(today=today, digest=DIGEST.upper()), today=today)
    assert upper.served, "a case-different digest is the same digest"


def test_extension_binds_the_run_id():
    today = dt.date(2026, 9, 8)
    v = ev(_payload(today=today, run_id="20260905T110003Z"), today=today)
    assert not v.served and "not licensed" in v.reason
    assert a4t1_window_extension("20260905T110003Z",
                                 {"fallback_a4t1_candidate_digest": DIGEST}, today) is None


def test_extension_does_not_rescue_any_other_refusal():
    today = dt.date(2026, 9, 8)
    # RFC#210 underneath: an aged-out artifact stays refused
    assert not ev(_payload(today=today, trained_days_old=29), today=today).served
    # override flag must still be literally True
    p = _payload(today=today); p["metadata"]["fallback_a4t1_override"] = "true"
    assert not ev(p, today=today).served
    # the orchestrator receipt is still mandatory
    p = _payload(today=today); del p["metadata"]["fallback_a4t1_consumption_proof"]
    v = ev(p, today=today)
    assert not v.served and "receipt" in v.reason


def test_an_empty_registry_restores_the_standing_close(monkeypatch):
    monkeypatch.setattr(lic, "A4T1_WINDOW_EXTENSIONS", ())
    today = dt.date(2026, 9, 8)
    v = ev(_payload(today=today), today=today)
    assert not v.served and "window closed" in v.reason


# ── the preflight twins see it ────────────────────────────────────────────

def test_both_preflight_twins_admit_the_extended_artifact(tmp_path, monkeypatch):
    import json
    from renquant_pipeline.kernel import preflight as kp
    from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
    from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import RegimeLayeredICTask

    today = dt.date(2026, 9, 8)

    class _Frozen(dt.date):
        @classmethod
        def today(cls):
            return cls(today.year, today.month, today.day)
    monkeypatch.setattr(lic.dt, "date", _Frozen)

    p = tmp_path / "artifacts" / "prod" / "panel-ltr.alpha158_fund.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(_payload(today=today)), encoding="utf-8")

    mono = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    task = RegimeLayeredICTask().check(
        PreflightContext(config=_config(), strategy_dir=tmp_path, run_mode="full"))
    assert mono.ok is True and mono.severity == "soft", (mono.severity, mono.message)
    assert mono.message.startswith("LICENSED (RFC#210 A4-T1)")
    assert mono.message == task.message
    assert mono.details["rfc210_a4t1_license"]["a4t1_window_extended"] is True
