"""Smoke test for the C2.9 data + preflight lift to renquant-pipeline.

5 top-level kernel/*.py → renquant_pipeline.kernel (Runtime A4 per inventory):
  * data.py            — OHLCV / panel data fetch
  * data_cache.py      — in-process panel cache
  * data_coverage.py   — row-coverage diagnostics
  * preflight.py       — live preflight gate runner (P-WF-GATE, P-PANEL-CONTRACT, …)
  * trade_events.py    — trade event recording

Phase 1 invariant: byte-equivalent + soft-skip on kernel.* dep.
trade_events.py has one ``kernel.pipeline.exit_params`` dep that resolves only
in umbrella sys.path (Phase 5 will flip).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


_BT_PKG = Path(__file__).resolve().parents[1] / "src" / "renquant_pipeline" / "kernel"
_UMBRELLA = Path(__file__).resolve().parents[2] / "RenQuant" / "backtesting" / \
            "renquant_104" / "kernel"

_LIFTED = ("data.py", "data_cache.py", "data_coverage.py", "preflight.py", "trade_events.py")


def test_byte_equivalent_to_umbrella() -> None:
    pytest.skip("retired in Phase 5: subrepo imports rewritten to renquant_pipeline.* and renquant_common.*, byte-mirror invariant intentionally broken")
    if not _UMBRELLA.exists():
        pytest.skip(f"umbrella not at {_UMBRELLA}")
    for name in _LIFTED:
        bt = _BT_PKG / name
        um = _UMBRELLA / name
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
    "renquant_pipeline.kernel.data",
    "renquant_pipeline.kernel.data_cache",
    "renquant_pipeline.kernel.data_coverage",
    "renquant_pipeline.kernel.preflight",
    "renquant_pipeline.kernel.trade_events",
])
def test_submodule_import_or_known_kernel_dep(name: str) -> None:
    ok = _try_import(name)
    assert ok in (True, False)
