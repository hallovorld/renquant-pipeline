"""Phase 1 byte-equivalence smoke test for ``kernel/preflight_pipeline/`` lift.

17 files (4 module + 13 task) → ``renquant_pipeline.kernel.preflight_pipeline``.
Houses the T/J/P architecture for the 16 preflight checks introduced in
umbrella PRs #7+#8 (Track H complete).

Phase 1 invariant: byte-equivalent text mirror of umbrella files. Many
submodules import ``kernel.*`` in absolute form so they soft-skip standalone
import; Phase 5 will retire those imports.

This test is parallel to ``test_c211_panel_pipeline_lift.py`` for the
``panel_pipeline`` lift.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


_BT_PKG = Path(__file__).resolve().parents[1] / "src" / "renquant_pipeline" / "kernel" / "preflight_pipeline"
_UMBRELLA = (Path(__file__).resolve().parents[2] / "RenQuant" / "backtesting"
             / "renquant_104" / "kernel" / "preflight_pipeline")


def test_byte_equivalent_to_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    """Every .py file in the lift package must MD5-match its umbrella twin."""
    if not _UMBRELLA.exists():
        pytest.skip(f"umbrella not at {_UMBRELLA}")
    seen = 0
    for f in sorted(_BT_PKG.rglob("*.py")):
        # Skip __pycache__
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(_BT_PKG)
        u = _UMBRELLA / rel
        if not u.exists():
            continue
        assert hashlib.md5(f.read_bytes()).hexdigest() == hashlib.md5(u.read_bytes()).hexdigest(), \
            f"byte-mismatch: {rel}"
        seen += 1
    # 4 module files (__init__.py, ctx.py, base.py, pipeline.py)
    # + 13 task files (state, broker, artifact, gate, sector_map, watchlist,
    #   correlation, calibrator, feature_coverage, run_id, config_fingerprint,
    #   meta_label, plus tasks/__init__.py)
    assert seen >= 16, f"expected ≥16 lifted, saw {seen}"


def test_expected_files_present() -> None:
    """Pin the file inventory so a missed lift fails loudly."""
    expected_module = {
        "__init__.py", "ctx.py", "base.py", "pipeline.py",
    }
    expected_tasks = {
        "__init__.py",
        "state.py", "broker.py",
        "artifact.py",
        "gate.py",
        "sector_map.py", "watchlist.py", "correlation.py",
        "calibrator.py",
        "feature_coverage.py", "run_id.py",
        "config_fingerprint.py", "meta_label.py",
    }
    module_files = {f.name for f in _BT_PKG.glob("*.py")}
    task_files = {f.name for f in (_BT_PKG / "tasks").glob("*.py")}
    assert expected_module <= module_files, \
        f"missing module files: {expected_module - module_files}"
    assert expected_tasks <= task_files, \
        f"missing task files: {expected_tasks - task_files}"


def test_preflight_py_matches_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    """``kernel/preflight.py`` is also part of this lift (PR #8 wrapper change).

    The wrapper change replaces ``run_preflight``'s body with a thin call to
    ``PreflightPipeline.run()`` + a result sort by ``_LEGACY_CHECK_ORDER``.
    Subrepo bytes must match umbrella bytes.
    """
    sub = _BT_PKG.parent / "preflight.py"
    umb = _UMBRELLA.parent / "preflight.py"
    if not umb.exists():
        pytest.skip(f"umbrella preflight.py not at {umb}")
    assert hashlib.md5(sub.read_bytes()).hexdigest() == \
        hashlib.md5(umb.read_bytes()).hexdigest(), \
        f"byte-mismatch: preflight.py"


def test_job_panel_scoring_matches_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    """``panel_pipeline/job_panel_scoring.py`` carries the (d) bypass change.

    Subrepo must match umbrella so both repos honor the same
    ``RQ_SIM_BYPASS_BUY_FLOOR=1`` env-flag contract.
    """
    sub = _BT_PKG.parent / "panel_pipeline" / "job_panel_scoring.py"
    umb = _UMBRELLA.parent / "panel_pipeline" / "job_panel_scoring.py"
    if not umb.exists():
        pytest.skip(f"umbrella job_panel_scoring.py not at {umb}")
    assert hashlib.md5(sub.read_bytes()).hexdigest() == \
        hashlib.md5(umb.read_bytes()).hexdigest(), \
        f"byte-mismatch: panel_pipeline/job_panel_scoring.py"
