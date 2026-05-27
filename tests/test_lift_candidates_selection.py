"""Parity tests for candidates/ranking/selection/rotation/topup/universe lift.

Copy-and-rewrite. Includes the kernel/ support modules state_paths +
persistence (the latter only via a lazy task_benchmark_sleeve import).
Pins import-cleanliness, the no-bare-kernel rewrite, and that the lifted Jobs
wire chains of real common.Task subclasses.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import renquant_common

PKG = "renquant_pipeline.kernel.pipeline."
KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"

PIPELINE_MODULES = [
    "job_candidates",
    "task_candidates",
    "job_ranking",
    "task_ranking",
    "job_selection",
    "task_selection",
    "job_rotation",
    "task_rotation",
    "task_topup",
    "job_universe",
]
KERNEL_MODULES = ["state_paths", "persistence"]


@pytest.mark.parametrize("module_name", PIPELINE_MODULES)
def test_pipeline_module_imports(module_name: str) -> None:
    assert importlib.import_module(PKG + module_name) is not None


@pytest.mark.parametrize("module_name", KERNEL_MODULES)
def test_kernel_module_imports(module_name: str) -> None:
    assert importlib.import_module("renquant_pipeline.kernel." + module_name) is not None


def test_no_bare_kernel_import_survives() -> None:
    rel = [f"pipeline/{m}.py" for m in PIPELINE_MODULES] + [f"{m}.py" for m in KERNEL_MODULES]
    offenders: list[str] = []
    for r in rel:
        tree = ast.parse((KERNEL / r).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".", 1)[0] == "kernel":
                    offenders.append(f"{r}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(f"{r}: import {alias.name}")
    assert offenders == [], f"un-rewritten bare kernel imports: {offenders}"


def test_jobs_wire_task_chains() -> None:
    specs = [
        ("job_candidates", "TickerCandidateJob"),
        ("job_ranking", "RankingJob"),
        ("job_selection", "SelectionJob"),
        ("job_rotation", "RotationJob"),
    ]
    for mod_name, cls_name in specs:
        mod = importlib.import_module(PKG + mod_name)
        tasks = getattr(mod, cls_name)().tasks
        assert isinstance(tasks, list) and tasks, f"{cls_name} has no tasks"
        for t in tasks:
            assert isinstance(t, renquant_common.Task), f"{cls_name}: {t!r} not a Task"
