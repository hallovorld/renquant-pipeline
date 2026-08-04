from __future__ import annotations

from pathlib import Path

from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import WfGateMetadataTask


def _wf_metadata(*, diagnostic_only: bool) -> dict:
    return {
        "passed": True,
        "diagnostic_only": diagnostic_only,
        "wf_3cut_sharpe_mean": 1.2,
        "wf_3cut_apy_mean": 0.2,
        "spy_sharpe_mean": 0.8,
        "strategy_minus_spy_sharpe_mean": 0.4,
        "n_cuts_beat_spy_sharpe": 3,
        "sanity_regime_ic": {"passed": True},
    }


def test_diagnostic_only_wf_evidence_hard_blocks_full_runs(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")

    result = WfGateMetadataTask()._evaluate_wf(_wf_metadata(diagnostic_only=True), ctx)

    assert result.name == "P-WF-GATE"
    assert result.severity == "hard"
    assert result.ok is False
    assert result.details["diagnostic_only"] is True


def test_diagnostic_only_wf_evidence_preserves_sell_only_exits(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="sell_only")

    result = WfGateMetadataTask()._evaluate_wf(_wf_metadata(diagnostic_only=True), ctx)

    assert result.name == "P-WF-GATE"
    assert result.severity == "soft"
    assert result.ok is True


# --- RFC #210 governance license (2026-08-04 sell-only incident) ---------------
#
# The first freshness-fallback promotion stamped passed=False by design and
# P-WF-GATE hard-failed the full run: the book went sell-only on the new
# model's first day. Both twins (this Task and kernel.preflight's monolith
# check) must admit a governance-served artifact while it stays fresh, and
# ONLY then.

import datetime as _dt
import json as _json

from renquant_pipeline.kernel import preflight as _kp


def _failed_wf() -> dict:
    return {
        "passed": False,
        "wf_3cut_sharpe_mean": 0.6017,
        "spy_sharpe_mean": 1.0808,
        "wf_reason": "FAIL: benchmark_ok=False",
    }


def _licensed_payload(days_old: int = 2) -> dict:
    trained = (_dt.date.today() - _dt.timedelta(days=days_old)).isoformat()
    return {
        "trained_date": trained,
        "metadata": {
            "promotion_basis": "freshness_fallback_rfc210",
            "wf_gate_metadata": _failed_wf(),
        },
    }


def test_governance_served_artifact_admits_full_run(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    result = WfGateMetadataTask()._evaluate_wf(
        _failed_wf(), ctx, payload=_licensed_payload())
    assert result.name == "P-WF-GATE"
    assert result.severity == "hard"
    assert result.ok is True
    assert result.details["freshness_fallback_rfc210"]["age_days"] == 2


def test_governance_license_refused_when_aged_out(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    result = WfGateMetadataTask()._evaluate_wf(
        _failed_wf(), ctx, payload=_licensed_payload(days_old=44))
    assert result.ok is False and result.severity == "hard"
    assert "aged out" in result.details["freshness_fallback_rfc210_refused"]


def test_plain_gate_fail_without_license_still_hard_blocks(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    result = WfGateMetadataTask()._evaluate_wf(_failed_wf(), ctx, payload=None)
    assert result.ok is False and result.severity == "hard"


def test_sell_only_path_unchanged_by_the_license(tmp_path: Path) -> None:
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="sell_only")
    result = WfGateMetadataTask()._evaluate_wf(
        _failed_wf(), ctx, payload=_licensed_payload())
    assert result.severity == "soft" and result.ok is True


def _write_artifact(tmp_path: Path, payload: dict) -> None:
    p = tmp_path / "artifacts" / "prod" / "panel-ltr.alpha158_fund.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(payload), encoding="utf-8")


def test_monolith_twin_admits_the_same_license_end_to_end(tmp_path: Path) -> None:
    # The OTHER twin — kernel.preflight._check_wf_gate_metadata — read from a
    # real artifact file, exactly as the live runner resolves it.
    _write_artifact(tmp_path, _licensed_payload())
    result = _kp._check_wf_gate_metadata({}, tmp_path, run_mode="full")
    assert result.name == "P-WF-GATE"
    assert result.severity == "hard" and result.ok is True
    assert result.details["freshness_fallback_rfc210"]["promotion_basis"] == (
        "freshness_fallback_rfc210")


def test_monolith_twin_still_blocks_unlicensed_gate_fail(tmp_path: Path) -> None:
    payload = _licensed_payload()
    payload["metadata"]["promotion_basis"] = "not_a_license"
    _write_artifact(tmp_path, payload)
    result = _kp._check_wf_gate_metadata({}, tmp_path, run_mode="full")
    assert result.ok is False and result.severity == "hard"
