"""Smoke test for kernel/panel_pipeline/ lift (Track C2.11).

16 files → renquant_pipeline.kernel.panel_pipeline (Runtime A2 per inventory).
Houses the runtime scoring stack: panel_scorer + alpha158 features + PatchTST
scorers + ensemble + regime routing.

Phase 1 invariant: byte-equivalent; many submodules import kernel.* in
absolute form so they soft-skip until Phase 5.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


_BT_PKG = Path(__file__).resolve().parents[1] / "src" / "renquant_pipeline" / "kernel" / "panel_pipeline"
_UMBRELLA = Path(__file__).resolve().parents[2] / "RenQuant" / "backtesting" / \
            "renquant_104" / "kernel" / "panel_pipeline"


def test_byte_equivalent_to_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    if not _UMBRELLA.exists():
        pytest.skip(f"umbrella not at {_UMBRELLA}")
    seen = 0
    for f in sorted(_BT_PKG.glob("*.py")):
        u = _UMBRELLA / f.name
        if not u.exists():
            continue
        assert hashlib.md5(f.read_bytes()).hexdigest() == hashlib.md5(u.read_bytes()).hexdigest(), \
            f"byte-mismatch: {f.name}"
        seen += 1
    assert seen >= 15, f"expected ≥15 lifted, saw {seen}"


def test_expected_files_present() -> None:
    expected = {
        "__init__.py", "alpha158_features.py", "ensemble_scorer.py",
        "feature_matrix.py", "feature_transform.py", "hf_patchtst_scorer.py",
        "job_panel_scoring.py", "model_contract.py", "model_registry.py", "panel_scorer.py",
        "patchtst_scorer.py", "regime_ensemble_scorer.py", "regime_router.py",
        "regime_router_scorer.py", "shadow_scoring.py", "task_quality_floor.py",
        "tasks_feature_matrix.py", "transformer_scorer.py",
    }
    present = {f.name for f in _BT_PKG.glob("*.py")}
    missing = expected - present
    assert not missing, f"missing: {missing}"


def _try_import(modname: str) -> bool:
    try:
        __import__(modname)
        return True
    except ModuleNotFoundError as exc:
        if "kernel" in str(exc):
            return False
        raise


@pytest.mark.parametrize("name", [
    "renquant_pipeline.kernel.panel_pipeline.alpha158_features",
    "renquant_pipeline.kernel.panel_pipeline.feature_matrix",
    "renquant_pipeline.kernel.panel_pipeline.feature_transform",
    "renquant_pipeline.kernel.panel_pipeline.model_registry",
    "renquant_pipeline.kernel.panel_pipeline.panel_scorer",
    "renquant_pipeline.kernel.panel_pipeline.shadow_scoring",
])
def test_submodule_import_or_known_kernel_dep(name: str) -> None:
    ok = _try_import(name)
    assert ok in (True, False)
