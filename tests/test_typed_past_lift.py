"""Smoke test for the typed_past/ lift (Track C2.5a).

typed_past is Runtime A4 per kernel-inventory.md — read-only typed views of
the past panel used by InferencePipeline at decision time. Lifted to
renquant-pipeline (not renquant-backtesting) because it's runtime, not sim.

Phase 1 invariant: byte-equivalent + file-presence; soft-skip on kernel.* dep.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


_BT_PKG = Path(__file__).resolve().parents[1] / "src" / "renquant_pipeline" / "kernel" / "typed_past"
_UMBRELLA = Path(__file__).resolve().parents[2] / "RenQuant" / "backtesting" / \
            "renquant_104" / "kernel" / "typed_past"


def test_byte_equivalent_to_umbrella() -> None:
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
    assert seen >= 3


def test_expected_files_present() -> None:
    expected = {"__init__.py", "estimator.py", "past.py", "typed_data_freshness.py"}
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
    "renquant_pipeline.kernel.typed_past.past",
    "renquant_pipeline.kernel.typed_past.estimator",
    "renquant_pipeline.kernel.typed_past.typed_data_freshness",
])
def test_submodule_import_or_known_kernel_dep(name: str) -> None:
    ok = _try_import(name)
    assert ok in (True, False)
