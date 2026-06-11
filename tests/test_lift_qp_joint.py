"""Parity tests for joint-actions + QP Tasks/Jobs (functional-lift).

Copy-and-rewrite. Includes a guards-only trimmed renquant_pipeline.kernel.
walk_forward (loader/manifest excluded — they pull panel_pipeline/xgboost,
which belongs in renquant-model / renquant-backtesting). Pins
import-cleanliness, the no-bare-kernel rewrite, and that the QP/joint Jobs
wire chains of real common.Task subclasses.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import renquant_common

KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"
MODULES = {
    "pipeline/job_joint_actions.py": "renquant_pipeline.kernel.pipeline.job_joint_actions",
    "pipeline/task_joint_actions.py": "renquant_pipeline.kernel.pipeline.task_joint_actions",
    "portfolio_qp/job_qp.py": "renquant_pipeline.kernel.portfolio_qp.job_qp",
    "portfolio_qp/task_joint_qp.py": "renquant_pipeline.kernel.portfolio_qp.task_joint_qp",
    "portfolio_qp/tasks.py": "renquant_pipeline.kernel.portfolio_qp.tasks",
    "walk_forward/correlation_guard.py": "renquant_pipeline.kernel.walk_forward.correlation_guard",
}


@pytest.mark.parametrize("module_name", sorted(set(MODULES.values())))
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_walk_forward_is_guards_only() -> None:
    """The lifted walk_forward must NOT pull the model-artifact loader/manifest."""
    wf = importlib.import_module("renquant_pipeline.kernel.walk_forward")
    assert hasattr(wf, "assert_correlation_no_leakage")
    assert not hasattr(wf, "WalkForwardModelLoader"), (
        "loader/manifest must stay out of the pipeline (panel_pipeline/xgboost)"
    )


def test_no_bare_kernel_import_survives() -> None:
    offenders: list[str] = []
    for rel in MODULES:
        tree = ast.parse((KERNEL / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".", 1)[0] == "kernel":
                    offenders.append(f"{rel}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(f"{rel}: import {alias.name}")
    assert offenders == [], f"un-rewritten bare kernel imports: {offenders}"


def test_qp_and_joint_jobs_wire_task_chains() -> None:
    jja = importlib.import_module("renquant_pipeline.kernel.pipeline.job_joint_actions")
    jqp = importlib.import_module("renquant_pipeline.kernel.portfolio_qp.job_qp")
    for job_cls in (jja.JointActionJob, jqp.JointPortfolioQPJob):
        tasks = job_cls().tasks
        assert isinstance(tasks, list) and tasks, f"{job_cls.__name__} has no tasks"
        for t in tasks:
            assert isinstance(t, renquant_common.Task)


def _qp_admission_reason(gate: dict, *, regime: str = "CHOPPY", source=None) -> str | None:
    from renquant_pipeline.kernel.portfolio_qp.tasks import _qp_buy_admission_block_reason

    if source is None:
        source = SimpleNamespace(
            ticker="AAPL",
            rank_score=1.0,
            panel_score=1.0,
            sigma=0.20,
            expected_return=0.02,
            expected_return_horizon_days=5,
            mu=0.02,
            mu_horizon_days=5,
        )
    ctx = SimpleNamespace(regime=regime, config={})
    env = {
        "cfg": {
            "qp_mu_horizon_days": 5,
            "qp_admission_gate": {"enabled": True, "respect_open_slots": False, **gate},
        },
        "holdings_set": set(),
        "score_sources": {"AAPL": source},
        "ignore_slots": True,
    }
    return _qp_buy_admission_block_reason(ctx, env, "AAPL")


def test_qp_admission_expected_return_by_regime_missing_regime_fails_closed() -> None:
    reason = _qp_admission_reason({
        "min_expected_return_by_regime": {"BULL_CALM": 0.01},
    })

    assert reason == "qp_admission_expected_return_missing_regime"


def test_qp_admission_sigma_by_regime_missing_regime_fails_closed() -> None:
    reason = _qp_admission_reason({
        "max_sigma_by_regime": {"BULL_CALM": 0.30},
    })

    assert reason == "qp_admission_sigma_missing_regime"


def test_qp_admission_expected_return_over_sigma_missing_regime_fails_closed() -> None:
    reason = _qp_admission_reason({
        "min_expected_return_over_sigma_by_regime": {"BULL_CALM": 0.05},
    })

    assert reason == "qp_admission_expected_return_over_sigma_missing_regime"


def test_qp_admission_by_regime_allows_explicit_global_fallback() -> None:
    gate = {
        "min_expected_return_by_regime": {"BULL_CALM": 0.01},
        "min_expected_return": 0.005,
    }

    assert _qp_admission_reason(gate) is None
    assert _qp_admission_reason(
        gate,
        source=SimpleNamespace(
            ticker="AAPL",
            rank_score=1.0,
            panel_score=1.0,
            sigma=0.20,
            expected_return=0.004,
            expected_return_horizon_days=5,
            mu=0.004,
            mu_horizon_days=5,
        ),
    ) == "qp_admission_expected_return"
