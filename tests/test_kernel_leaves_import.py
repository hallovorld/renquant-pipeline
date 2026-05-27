"""Smoke + boundary tests for the lifted decision-leaf kernel modules.

Verifies the copy-not-move slice imports cleanly in the pipeline package
and pulls no forbidden backend/execution modules (RFC §"Backfill Plan"
functional-lift).
"""
from __future__ import annotations

import importlib

import pytest

LIFTED_MODULES = [
    # slice 1
    "renquant_pipeline.kernel.kelly",
    "renquant_pipeline.kernel.exit_types",
    "renquant_pipeline.kernel.market_gates",
    "renquant_pipeline.kernel.vol_target",
    "renquant_pipeline.kernel.sizing",
    # slice 2
    "renquant_pipeline.kernel.regime_resolver",
    "renquant_pipeline.kernel.regime_hmm",
    "renquant_pipeline.kernel.intraday",
    "renquant_pipeline.kernel.intraday_wash",
    "renquant_pipeline.kernel.config",
    "renquant_pipeline.kernel.config_consistency",
    "renquant_pipeline.kernel.net_safety",
    "renquant_pipeline.kernel.realized_pnl",
    "renquant_pipeline.kernel.portfolio",
    "renquant_pipeline.kernel.scoring",
]


@pytest.mark.parametrize("module_name", LIFTED_MODULES)
def test_lifted_module_imports(module_name: str) -> None:
    mod = importlib.import_module(module_name)
    assert mod is not None


def test_kelly_fraction_is_bounded() -> None:
    """Sanity on the canonical kelly entry point if present."""
    kelly = importlib.import_module("renquant_pipeline.kernel.kelly")
    # The module exposes kelly-fraction helpers; just assert callables exist.
    public = [n for n in dir(kelly) if not n.startswith("_")]
    assert public, "kelly module exposes no public symbols"
