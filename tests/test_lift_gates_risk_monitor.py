"""Parity tests for the gates / risk / monitor Tasks + Jobs (functional-lift).

Copy-and-rewrite slice. Closures were already satisfied (config / market_gates
/ exit_types / exits + context/pipeline). Pins import-cleanliness, the
no-bare-kernel-import rewrite, and that the lifted Jobs wire task chains of
real common.Task subclasses.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import renquant_common

PKG = "renquant_pipeline.kernel.pipeline."
KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"

MODULES = [
    "job_gates",
    "job_panel_veto",
    "task_gates",
    "task_risk_gates",
    "task_data_freshness",
    "task_monitor",
    "task_buy_quality_gates",
    "task_trim",
    "task_limit_sells",
    "task_panel_veto",
    "task_post_stop_cooldown",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(PKG + module_name) is not None


def test_no_bare_kernel_import_survives() -> None:
    offenders: list[str] = []
    for m in MODULES:
        tree = ast.parse((KERNEL / "pipeline" / f"{m}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".", 1)[0] == "kernel":
                    offenders.append(f"{m}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(f"{m}: import {alias.name}")
    assert offenders == [], f"un-rewritten bare kernel imports: {offenders}"


def test_jobs_wire_task_chains() -> None:
    job_gates = importlib.import_module(PKG + "job_gates")
    job_panel_veto = importlib.import_module(PKG + "job_panel_veto")
    for job_cls in (job_gates.BuyGatesJob, job_panel_veto.PanelRankVetoJob):
        tasks = job_cls().tasks
        assert isinstance(tasks, list) and tasks, f"{job_cls.__name__} has no tasks"
        for t in tasks:
            assert isinstance(t, renquant_common.Task), (
                f"{job_cls.__name__} task {t!r} is not a common.Task"
            )
