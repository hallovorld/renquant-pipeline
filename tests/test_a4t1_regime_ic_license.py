"""RFC#210 Amendment A4-T1: P-REGIME-IC for the ONE authorized zero-trade candidate.

2026-09-03: the operator-authorized candidate 20260831T141820Z was promoted
under RFC#210 (renquant-backtesting#128 / renquant-orchestrator#1110 /
RenQuant#632). Its WF produced no round-trips, so `trade_monotonicity` carries
no eligible regime and P-REGIME-IC hard-failed the full daily run ("no regime
has enough OOS trades") — the served model could exit but never buy. This
license admits exactly that artifact, for exactly the stamped window, and
refuses every malformed twin toward the standing hard fail.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from renquant_pipeline.kernel import preflight as kp
from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import RegimeLayeredICTask
from renquant_pipeline.kernel.rfc210_license import (
    A4T1_LICENSED_RUN_IDS,
    evaluate_a4t1_regime_evidence_license as ev,
)

RUN_ID = "20260831T141820Z"
TODAY = dt.date(2026, 9, 4)
RECEIPT = "2cd9d27b0b96835119827de0760213a0539e71ac7574c213a74f68a5cc772d6e"


def _zero_trade_wf() -> dict:
    """The stamped WF block of the real candidate: rejected, zero trades, no regimes."""
    return {
        "passed": False,
        "diagnostic_only": False,
        "wf_reason": ("FAIL: zero trades across all WF cuts; decision tree admitted "
                      "no buys, so Sharpe is undefined and SPY benchmark cannot be met"),
        "sanity_placebo_genuine_ic": 0.001553838965806277,
        "sanity_regime_ic": {"passed": False,
                             "reason": "regime sanity IC failed: BULL_CALM,BULL_VOLATILE,CHOPPY"},
        "trade_contract": {"passed": False, "reason": "no round-trip ledgers found"},
        "trade_monotonicity": {"passed": False, "reason": "no round-trip ledgers found"},
        "alpha_economics": {"passed": False, "reason": "no round-trip ledgers found"},
    }


def _payload(*, days_old: int = 4, expiry: str | None = None, today: dt.date = TODAY,
             receipt: str | None = RECEIPT, override: object = True,
             run_id: str = RUN_ID, basis: str = "freshness_fallback_rfc210") -> dict:
    trained = (today - dt.timedelta(days=days_old)).isoformat()
    exp = expiry if expiry is not None else (today + dt.timedelta(days=3)).isoformat()
    meta = {
        "promotion_basis": basis,
        "fallback_genuine_ic": 0.001553838965806277,
        "fallback_quality_floor": 0.001,
        "fallback_a4t1_override": override,
        "fallback_a4t1_expiry": exp,
        "fallback_a4t1_authorization": "orch-session-428feb92-2026-08-31",
        "fallback_a4t1_candidate_run_id": run_id,
        "fallback_a4t1_candidate_artifact_digest": "760912ec" + "0" * 56,
        "fallback_a4t1_candidate_authority":
            "renquant-orchestrator:ops/governance/a4t1/20260831T141820Z.authorization.json",
        "wf_gate_metadata": _zero_trade_wf(),
    }
    if receipt is not None:
        meta["fallback_a4t1_consumption_proof"] = {
            "schema": "a4t1_consumption_proof.v1", "receipt_id": receipt,
            "consumed_by": "renquant-orchestrator",
        }
    return {"kind": "panel_ltr_xgboost", "trained_date": trained,
            "feature_cols": ["f1"], "metadata": meta}


def _config() -> dict:
    """The pinned production shape: regime admission off, sanity IC relaxed."""
    return {
        "ranking": {"panel_scoring": {
            "enabled": True, "kind": "xgb",
            "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
            "regime_admission": {"enabled": False},
        }},
        "wf_gate": {"sanity_regime_ic_required": False},
    }


def _write(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "artifacts" / "prod" / "panel-ltr.alpha158_fund.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


# ── the license itself ─────────────────────────────────────────────────────

def test_the_promoted_candidate_shape_is_served():
    v = ev(_payload(), today=TODAY)
    assert v.served, v.reason
    assert v.provenance["a4t1_candidate_run_id"] == RUN_ID
    assert v.provenance["a4t1_days_left"] == 3
    assert v.provenance["a4t1_receipt_id"] == RECEIPT
    assert v.provenance["promotion_basis"] == "freshness_fallback_rfc210"


def test_only_the_one_run_id_is_licensed():
    assert A4T1_LICENSED_RUN_IDS == frozenset({RUN_ID})
    v = ev(_payload(run_id="20260901T120000Z"), today=TODAY)
    assert not v.served and "not licensed" in v.reason


def test_window_closes_at_the_stamped_expiry():
    at = ev(_payload(expiry=TODAY.isoformat()), today=TODAY)
    past = ev(_payload(expiry=(TODAY - dt.timedelta(days=1)).isoformat()), today=TODAY)
    assert at.served and at.provenance["a4t1_days_left"] == 0
    assert not past.served and "window closed" in past.reason


def test_missing_or_empty_receipt_refuses():
    assert "receipt" in ev(_payload(receipt=None), today=TODAY).reason
    assert "receipt" in ev(_payload(receipt=""), today=TODAY).reason


def test_override_flag_must_be_literally_true():
    for bad in (False, None, "true", 1):
        v = ev(_payload(override=bad), today=TODAY)
        assert not v.served and "fallback_a4t1_override" in v.reason


def test_requires_the_rfc210_freshness_license_underneath():
    aged = ev(_payload(days_old=29), today=TODAY)
    assert not aged.served and "aged out" in aged.reason
    basis = ev(_payload(basis="manual_promote"), today=TODAY)
    assert not basis.served and "promotion_basis" in basis.reason


def test_malformed_expiry_refuses():
    for bad in ("", "soon", None):
        p = _payload()
        p["metadata"]["fallback_a4t1_expiry"] = bad
        assert not ev(p, today=TODAY).served


# ── P-REGIME-IC in the full daily run ──────────────────────────────────────

def test_full_run_admits_the_licensed_zero_trade_candidate_as_SOFT_and_says_so(tmp_path):
    _write(tmp_path, _payload(today=dt.date.today()))
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    assert res.ok is True and res.severity == "soft", (res.severity, res.message)
    assert res.message.startswith("LICENSED (RFC#210 A4-T1): regime-layered OOS evidence ABSENT")
    assert RUN_ID in res.message and "WITHOUT regime IC proof" in res.message
    assert "passed for eligible regimes" not in res.message
    assert res.details["rfc210_a4t1_license"]["a4t1_candidate_run_id"] == RUN_ID
    assert res.details["eligible_regimes"] == []


def test_full_run_still_hard_fails_an_unlicensed_zero_trade_artifact(tmp_path):
    p = _payload(today=dt.date.today())
    for k in list(p["metadata"]):
        if k.startswith("fallback_a4t1_"):
            del p["metadata"][k]
    _write(tmp_path, p)
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    assert res.ok is False and res.severity == "hard"
    assert res.message.startswith("no regime has enough OOS trades")
    assert "fallback_a4t1_override" in res.details["rfc210_a4t1_license_refused"]


def test_full_run_hard_fails_once_the_window_has_closed(tmp_path):
    _write(tmp_path, _payload(today=dt.date.today(), expiry="2026-09-01"))
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    assert res.ok is False and res.severity == "hard"
    assert "window closed" in res.details["rfc210_a4t1_license_refused"]


def test_full_run_hard_fails_without_the_orchestrator_receipt(tmp_path):
    _write(tmp_path, _payload(today=dt.date.today(), receipt=None))
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    assert res.ok is False and res.severity == "hard"


def test_sell_only_run_is_unchanged(tmp_path):
    _write(tmp_path, _payload(today=dt.date.today()))
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="sell_only")
    assert res.ok is True and res.severity == "soft"
    assert res.message.startswith("LICENSED (RFC#210 A4-T1)")


def test_an_artifact_with_eligible_regimes_never_takes_the_license_path(tmp_path):
    p = _payload(today=dt.date.today())
    p["metadata"]["wf_gate_metadata"]["trade_monotonicity"] = {
        "passed": True, "pooled": {"n": 117, "spearman": 0.12},
        "regimes": [{"regime": "BULL_CALM", "eligible": True, "passed": True, "spearman": 0.12}],
    }
    p["metadata"]["wf_gate_metadata"]["sanity_regime_ic"] = {"passed": True}
    _write(tmp_path, p)
    res = kp._check_regime_layered_ic(_config(), tmp_path, run_mode="full")
    assert res.ok is True and "LICENSED" not in res.message
    assert "rfc210_a4t1_license" not in res.details


# ── the two twins must say the same thing ─────────────────────────────────

def _both(tmp_path: Path, run_mode: str = "full"):
    mono = kp._check_regime_layered_ic(_config(), tmp_path, run_mode=run_mode)
    ctx = PreflightContext(config=_config(), strategy_dir=tmp_path, run_mode=run_mode)
    task = RegimeLayeredICTask().check(ctx)
    return mono, task


def test_licensed_text_is_identical_in_both_twins(tmp_path):
    """The daily runner uses the task twin (its log lines are tagged
    preflight_pipeline); a license that only the legacy function knew would
    never reach production."""
    _write(tmp_path, _payload(today=dt.date.today()))
    mono, task = _both(tmp_path)
    assert mono.message == task.message
    assert mono.severity == task.severity == "soft" and mono.ok is task.ok is True
    assert task.details["rfc210_a4t1_license"] == mono.details["rfc210_a4t1_license"]


def test_refusal_reason_is_identical_in_both_twins(tmp_path):
    _write(tmp_path, _payload(today=dt.date.today(), expiry="2026-09-01"))
    mono, task = _both(tmp_path)
    assert mono.message == task.message and task.ok is False and task.severity == "hard"
    assert task.details["rfc210_a4t1_license_refused"] == mono.details["rfc210_a4t1_license_refused"]
