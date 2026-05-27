"""Parity tests for the sell-path Tasks + Job (functional-lift).

Copy-and-rewrite. Lifts TickerSellJob + task_sell / task_panel_conviction_xs /
task_dd_flatten (task_benchmark_sleeve + soft_exit_guards landed with the
support layer). Pins import-cleanliness, the no-bare-kernel rewrite, and that
TickerSellJob wires a chain of real common.Task subclasses.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import renquant_common

PKG = "renquant_pipeline.kernel.pipeline."
KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"
MODULES = ["job_sell", "task_sell", "task_panel_conviction_xs", "task_dd_flatten"]


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


def test_ticker_sell_job_wires_task_chain() -> None:
    job_sell = importlib.import_module(PKG + "job_sell")
    tasks = job_sell.TickerSellJob().tasks
    assert isinstance(tasks, list) and tasks
    for t in tasks:
        assert isinstance(t, renquant_common.Task)
