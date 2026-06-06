from __future__ import annotations

from pathlib import Path


def test_wf_gate_target_invokes_backtesting_package(tmp_path: Path) -> None:
    from renquant_pipeline.kernel.pipeline.pp_research_acceptance import (
        ResearchAcceptanceContext,
        ResearchAcceptancePipeline,
    )

    ctx = ResearchAcceptanceContext(
        repo=tmp_path,
        python="/python",
        targets=("wf-gate",),
        artifact=Path("artifacts/staging.json"),
        strategy_config="strategy_config.shadow.json",
        wf_jobs=7,
        dry_run=True,
    )

    ResearchAcceptancePipeline(targets=("wf-gate",)).run(ctx)

    assert len(ctx.executed) == 1
    spec = ctx.executed[0]
    assert spec.name == "wf_gate"
    assert spec.argv[:3] == ("/python", "-m", "renquant_backtesting.wf_gate")
    assert "scripts/run_wf_gate.py" not in " ".join(spec.argv)
    assert spec.argv[spec.argv.index("--artifact") + 1] == str(
        tmp_path / "artifacts" / "staging.json"
    )
    assert spec.argv[spec.argv.index("--strategy-config") + 1] == "strategy_config.shadow.json"
    assert spec.argv[spec.argv.index("--jobs") + 1] == "7"
    assert "--strict" in spec.argv
