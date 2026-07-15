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
