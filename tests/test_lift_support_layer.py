"""Parity tests for the decision-tree support layer (functional-lift).

Lifts the umbrella's authoritative support modules into the kernel mirror:
``decision_trace`` (kernel/), ``order_attribution`` / ``task_benchmark_sleeve``
/ ``soft_exit_guards`` / ``exit_params`` / ``order_dedupe`` (kernel/pipeline/).

NOTE: the pipeline repo also carries STALE bootstrap top-level
``decision_trace``/``order_attribution`` from P0-P3 smoke code. The
``renquant_pipeline.kernel.*`` versions lifted here are the authoritative
current-umbrella copies; the bootstrap top-level modules are superseded and
get removed at cutover.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"

SUPPORT_MODULES = [
    "renquant_pipeline.kernel.decision_trace",
    "renquant_pipeline.kernel.pipeline.order_attribution",
    "renquant_pipeline.kernel.pipeline.task_benchmark_sleeve",
    "renquant_pipeline.kernel.pipeline.soft_exit_guards",
    "renquant_pipeline.kernel.pipeline.exit_params",
    "renquant_pipeline.kernel.pipeline.order_dedupe",
]


@pytest.mark.parametrize("module_name", SUPPORT_MODULES)
def test_support_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


def test_no_bare_kernel_import_survives() -> None:
    rel = [
        "decision_trace.py",
        "pipeline/order_attribution.py",
        "pipeline/task_benchmark_sleeve.py",
        "pipeline/soft_exit_guards.py",
    ]
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


def test_order_attribution_contract_validation() -> None:
    """stamp_order_attribution enforces its contract (ctx-independent path)."""
    oa = importlib.import_module("renquant_pipeline.kernel.pipeline.order_attribution")
    with pytest.raises(ValueError):
        oa.stamp_order_attribution(
            {"order_type": "market"},
            ctx=None,
            source_job="J",
            source_task="T",
            acceptance_reason="",  # empty → must raise
        )
    with pytest.raises(ValueError):
        oa.stamp_order_attribution(
            {},  # missing order_type → must raise
            ctx=None,
            source_job="J",
            source_task="T",
            acceptance_reason="ok",
        )
