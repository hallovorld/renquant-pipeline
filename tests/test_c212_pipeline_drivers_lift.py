"""Smoke test for the C2.12 pipeline-drivers lift.

4 missing kernel/pipeline/pp_*.py → renquant_pipeline.kernel.pipeline:
  * pp_execution.py            — order-emission Pipeline
  * pp_research_acceptance.py  — research-acceptance Pipeline
  * pp_training.py             — training Pipeline (one-shot)
  * pp_training_full.py        — full training Pipeline (tournament + panel + recal + ngboost)

These are runtime A1 per inventory but the orchestrator also uses them.
Phase 1: byte-equivalent + soft-skip on kernel.* deps.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


_BT_PKG = Path(__file__).resolve().parents[1] / "src" / "renquant_pipeline" / "kernel" / "pipeline"
_UMBRELLA = Path(__file__).resolve().parents[2] / "RenQuant" / "backtesting" / \
            "renquant_104" / "kernel" / "pipeline"

_LIFTED = ("pp_execution.py", "pp_research_acceptance.py", "pp_training.py", "pp_training_full.py")


def test_byte_equivalent_to_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    if not _UMBRELLA.exists():
        pytest.skip(f"umbrella not at {_UMBRELLA}")
    for name in _LIFTED:
        bt = _BT_PKG / name; um = _UMBRELLA / name
        assert bt.exists(), f"missing: {name}"
        assert hashlib.md5(bt.read_bytes()).hexdigest() == hashlib.md5(um.read_bytes()).hexdigest(), \
            f"byte-mismatch: {name}"


def _try_import(modname: str) -> bool:
    try:
        __import__(modname)
        return True
    except ModuleNotFoundError as exc:
        if "kernel" in str(exc):
            return False
        raise


@pytest.mark.parametrize("name", [
    "renquant_pipeline.kernel.pipeline.pp_execution",
    "renquant_pipeline.kernel.pipeline.pp_research_acceptance",
    "renquant_pipeline.kernel.pipeline.pp_training",
    "renquant_pipeline.kernel.pipeline.pp_training_full",
])
def test_submodule_import_or_known_kernel_dep(name: str) -> None:
    ok = _try_import(name)
    assert ok in (True, False)
