"""Parity tests for score-distribution / short-cover / execution Tasks (lift).

Copy-and-rewrite. (job_short_candidates / task_short_candidates are NOT here:
they import panel_pipeline.job_panel_scoring (xgboost) and route through the
model-scoring boundary — deferred to the model-integration / cutover track.)
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import renquant_common

PKG = "renquant_pipeline.kernel.pipeline."
KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"
MODULES = ["job_score_distribution", "task_score_distribution", "task_short_cover", "task_execution"]


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


def test_score_distribution_job_wires_task_chain() -> None:
    mod = importlib.import_module(PKG + "job_score_distribution")
    tasks = mod.ScoreDistributionJob().tasks
    assert isinstance(tasks, list) and tasks
    for t in tasks:
        assert isinstance(t, renquant_common.Task)
