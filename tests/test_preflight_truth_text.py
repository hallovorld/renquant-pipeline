"""P-WF-GATE / P-REGIME-IC never print a bare ✓ for a licensed or relaxed gate.

2026-08-30 finding (RenQuant daily_104 full-run log): with the served
artifact trained 2026-08-02, ``wf_gate_metadata.passed=false``,
``promotion_basis=freshness_fallback_rfc210`` and the pinned config's
``wf_gate.sanity_regime_ic_required=false``, the preflight logged

    ✓ P-WF-GATE   ... governance-served under RFC#210 ... buys admitted ...
    ✓ P-REGIME-IC ... regime-layered IC/monotonicity passed for eligible regimes ['BULL_CALM']

although the stamp says the gate FAILED and BULL_CALM FAILED (ρ=0.002). The
check text must lead with LICENSED / RELAXED and the facts the state is about.
Both twins (Task and monolith) are asserted; the fixture is the served
artifact's metadata shape [VERIFIED 2026-08-30].
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from renquant_pipeline.kernel import preflight as kp
from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import (
    RegimeLayeredICTask,
    WfGateMetadataTask,
)
from renquant_pipeline.kernel.rfc210_license import (
    genuine_ic_from_payload,
    licensed_check_message,
    evaluate_freshness_fallback_license,
)


def _served_wf() -> dict:
    return {
        "passed": False,
        "diagnostic_only": False,
        "wf_3cut_sharpe_mean": 0.6017718060321567,
        "spy_sharpe_mean": 1.0808386653410664,
        "strategy_minus_spy_sharpe_mean": -0.4790668593089097,
        "n_cuts_beat_spy_sharpe": 1,
        "wf_reason": "FAIL: absolute_ok=True, benchmark_ok=False, regime_ok=False",
        "sanity_placebo_genuine_ic": 0.0028876304346270865,
        "sanity_regime_ic": {
            "passed": False,
            "reason": "regime sanity IC failed: BULL_CALM,BULL_VOLATILE,CHOPPY",
        },
        "trade_monotonicity": {
            "passed": False,
            "pooled": {"n": 117, "spearman": 0.03911372202282383},
            "regimes": [
                {"regime": "BULL_CALM", "n": 104, "eligible": True,
                 "passed": False, "spearman": 0.0023365233812373976},
                {"regime": "BULL_VOLATILE", "n": 11, "eligible": False,
                 "passed": False, "spearman": 0.27272727272727276},
            ],
        },
    }


def _served_payload(days_old: int = 26) -> dict:
    trained = (dt.date.today() - dt.timedelta(days=days_old)).isoformat()
    return {
        "kind": "panel_ltr_xgboost",
        "trained_date": trained,
        "feature_cols": ["f1"],
        "metadata": {
            "fallback_as_of": "2026-08-04",
            "fallback_genuine_ic": 0.0028876304346270865,
            "promotion_basis": "freshness_fallback_rfc210",
            "wf_gate_metadata": _served_wf(),
        },
    }


def _relaxed_config() -> dict:
    return {
        "ranking": {"panel_scoring": {
            "enabled": True, "kind": "xgb",
            "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
            "regime_admission": {"enabled": False},
        }},
        "wf_gate": {"sanity_regime_ic_required": False},
    }


def _strict_config() -> dict:
    cfg = _relaxed_config()
    cfg.pop("wf_gate")
    return cfg


def _write(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "artifacts" / "prod" / "panel-ltr.alpha158_fund.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


# ── P-WF-GATE: licensed ────────────────────────────────────────────────────

def test_licensed_message_leads_with_the_failed_gate_and_the_license_facts():
    payload = _served_payload(days_old=26)
    lic = evaluate_freshness_fallback_license(payload)
    assert lic.served
    msg = licensed_check_message(lic, _served_wf(), payload)
    assert msg.startswith("LICENSED: WF gate FAILED, genuine_ic=+0.0029, served age 26d ≤ 28")
    assert "promotion_basis=freshness_fallback_rfc210" in msg
    assert "reason=FAIL: absolute_ok=True" in msg
    assert "this is not a WF pass" in msg
    assert "buys admitted ONLY while the RFC#210 freshness license holds" in msg


def test_genuine_ic_reads_metadata_then_stamp_then_na():
    assert genuine_ic_from_payload(_served_payload()) == 0.0028876304346270865
    p = _served_payload()
    del p["metadata"]["fallback_genuine_ic"]
    assert genuine_ic_from_payload(p) == 0.0028876304346270865
    del p["metadata"]["wf_gate_metadata"]["sanity_placebo_genuine_ic"]
    assert genuine_ic_from_payload(p) is None
    assert genuine_ic_from_payload({"metadata": {"fallback_genuine_ic": "0.1"}}) is None
    assert genuine_ic_from_payload(None) is None
    lic = evaluate_freshness_fallback_license(p)
    assert "genuine_ic=n/a" in licensed_check_message(lic, {}, p)


def test_task_twin_prints_licensed_not_bare_pass(tmp_path: Path):
    ctx = PreflightContext(config=_relaxed_config(), strategy_dir=tmp_path, run_mode="full")
    res = WfGateMetadataTask()._evaluate_wf(_served_wf(), ctx, payload=_served_payload(26))
    assert res.ok is True and res.severity == "hard"
    assert res.message.startswith("LICENSED: WF gate FAILED, genuine_ic=+0.0029, served age 26d ≤ 28")
    assert res.details["freshness_fallback_rfc210"]["age_days"] == 26
    assert "WF gate passed" not in res.message


def test_monolith_twin_prints_the_same_licensed_text(tmp_path: Path):
    _write(tmp_path, _served_payload(26))
    res = kp._check_wf_gate_metadata(_relaxed_config(), tmp_path, run_mode="full")
    assert res.ok is True and res.severity == "hard"
    assert res.message.startswith("LICENSED: WF gate FAILED, genuine_ic=+0.0029, served age 26d ≤ 28")
    ctx = PreflightContext(config=_relaxed_config(), strategy_dir=tmp_path, run_mode="full")
    twin = WfGateMetadataTask()._evaluate_wf(_served_wf(), ctx, payload=_served_payload(26))
    assert twin.message == res.message


def test_aged_out_license_still_hard_fails_with_the_refusal(tmp_path: Path):
    _write(tmp_path, _served_payload(29))
    res = kp._check_wf_gate_metadata(_relaxed_config(), tmp_path, run_mode="full")
    assert res.ok is False and res.severity == "hard"
    assert "LICENSED" not in res.message
    assert "29d old > 28d RFC#210 serving SLA" in res.details["freshness_fallback_rfc210_refused"]


def test_genuinely_passed_gate_keeps_the_plain_pass_text(tmp_path: Path):
    wf = _served_wf()
    wf.update({"passed": True, "sanity_regime_ic": {"passed": True}})
    payload = _served_payload(26)
    payload["metadata"]["wf_gate_metadata"] = wf
    payload["metadata"].pop("promotion_basis")
    _write(tmp_path, payload)
    res = kp._check_wf_gate_metadata(_strict_config(), tmp_path, run_mode="full")
    assert res.ok is True
    assert res.message.startswith("WF gate passed: wf_sharpe_mean=0.6017718060321567")
    assert "LICENSED" not in res.message and "RELAXED" not in res.message


# ── P-REGIME-IC: relaxed ───────────────────────────────────────────────────

def test_relaxed_regime_ic_leads_with_the_relaxed_state_both_twins(tmp_path: Path):
    _write(tmp_path, _served_payload(26))
    mono = kp._check_regime_layered_ic(_relaxed_config(), tmp_path, run_mode="full")
    ctx = PreflightContext(config=_relaxed_config(), strategy_dir=tmp_path, run_mode="full")
    task = RegimeLayeredICTask().check(ctx)
    for res in (mono, task):
        assert res.ok is True and res.severity == "hard"
        assert res.message.startswith("RELAXED: ")
        assert "sanity IC failed (regime sanity IC failed: BULL_CALM,BULL_VOLATILE,CHOPPY)" in res.message
        assert "stamp failed BULL_CALM ρ=0.002" in res.message
        assert "sanity_regime_ic_required=false" in res.message
        assert "NOT proven for eligible regimes ['BULL_CALM']" in res.message
        assert "monotonicity passed" not in res.message
        assert res.details["sanity_regime_ic_relaxed"] is True
        assert res.details["trade_monotonicity_relaxed"] is True
    assert mono.message == task.message


def test_strict_config_still_blocks_the_same_regime_evidence(tmp_path: Path):
    _write(tmp_path, _served_payload(26))
    res = kp._check_regime_layered_ic(_strict_config(), tmp_path, run_mode="full")
    assert res.ok is False and res.severity == "hard"
    assert "RELAXED" not in res.message


def test_genuine_regime_pass_keeps_the_plain_pass_text(tmp_path: Path):
    payload = _served_payload(26)
    wf = payload["metadata"]["wf_gate_metadata"]
    wf["sanity_regime_ic"] = {"passed": True}
    wf["trade_monotonicity"] = {
        "passed": True,
        "pooled": {"n": 117, "spearman": 0.12},
        "regimes": [{"regime": "BULL_CALM", "eligible": True, "passed": True, "spearman": 0.12}],
    }
    _write(tmp_path, payload)
    res = kp._check_regime_layered_ic(_relaxed_config(), tmp_path, run_mode="full")
    assert res.ok is True
    assert res.message.startswith("regime-layered IC/monotonicity passed for eligible regimes ['BULL_CALM']")
    assert "RELAXED" not in res.message
