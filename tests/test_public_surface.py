"""Import-surface tests for renquant_pipeline.public (V-005 remediation).

Proves that importing the public module does NOT eagerly load kernel
subsystems — each symbol is lazy-loaded on first access only.

All checks run in a fresh subprocess so parent-process module cache
cannot mask real violations (codex review on this PR).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


def _run_script(script: str) -> dict:
    """Run *script* in a fresh interpreter and return the last JSON line.

    Hard-fails (not skip) on subprocess errors — codex review: a broken
    public contract must not be certified as "skipped".
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=60,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, (
        f"subprocess produced no JSON output "
        f"(exit={result.returncode}):\nstderr: {result.stderr}"
    )
    return json.loads(lines[-1])


def test_public_import_does_not_add_kernel_imports():
    """Importing renquant_pipeline.public must not load any kernel modules
    beyond what renquant_pipeline.__init__ already pulls in transitively."""
    data = _run_script("""
import json, sys
before = set(sys.modules)
import renquant_pipeline
after_pkg = set(sys.modules) - before
before2 = set(sys.modules)
import renquant_pipeline.public
after_public = set(sys.modules) - before2
kernel_from_pkg = sorted(n for n in after_pkg if n.startswith("renquant_pipeline.kernel"))
kernel_from_public = sorted(n for n in after_public if n.startswith("renquant_pipeline.kernel"))
print(json.dumps({"kernel_from_pkg": kernel_from_pkg, "kernel_from_public": kernel_from_public}))
""")
    assert data["kernel_from_public"] == [], (
        f"renquant_pipeline.public added kernel modules beyond what "
        f"renquant_pipeline.__init__ loads: {data['kernel_from_public']}"
    )


def test_public_lazy_access_loads_targeted_kernel_only():
    """Accessing LocalStore triggers kernel.data (and NOT kernel.exits or
    kernel.regime); accessing HoldingState triggers kernel.exits (and NOT
    kernel.data or kernel.regime). Each symbol loads only its own subsystem."""
    data = _run_script("""
import json, sys
import renquant_pipeline
import renquant_pipeline.public

baseline = set(sys.modules)
_ = renquant_pipeline.public.LocalStore
after_ls = set(sys.modules) - baseline

baseline2 = set(sys.modules)
_ = renquant_pipeline.public.HoldingState
after_hs = set(sys.modules) - baseline2

print(json.dumps({
    "after_localstore": sorted(n for n in after_ls if "kernel" in n),
    "after_holdingstate": sorted(n for n in after_hs if "kernel" in n),
}))
""")
    ls_modules = data["after_localstore"]
    hs_modules = data["after_holdingstate"]

    assert any("kernel.data" in m for m in ls_modules), (
        "accessing LocalStore did not load kernel.data"
    )
    assert not any("kernel.exits" in m for m in ls_modules), (
        f"accessing LocalStore loaded kernel.exits (cross-contamination): {ls_modules}"
    )
    assert not any("kernel.regime" in m for m in ls_modules), (
        f"accessing LocalStore loaded kernel.regime (cross-contamination): {ls_modules}"
    )

    assert any("kernel.exits" in m for m in hs_modules), (
        "accessing HoldingState did not load kernel.exits"
    )
    assert not any("kernel.regime" in m for m in hs_modules), (
        f"accessing HoldingState loaded kernel.regime (cross-contamination): {hs_modules}"
    )


def test_public_regime_state_loads_only_regime():
    """Accessing RegimeState triggers kernel.regime and nothing else."""
    data = _run_script("""
import json, sys
import renquant_pipeline
import renquant_pipeline.public

baseline = set(sys.modules)
_ = renquant_pipeline.public.RegimeState
after_rs = set(sys.modules) - baseline

print(json.dumps({
    "after_regimestate": sorted(n for n in after_rs if "kernel" in n),
}))
""")
    rs_modules = data["after_regimestate"]

    assert any("kernel.regime" in m for m in rs_modules), (
        "accessing RegimeState did not load kernel.regime"
    )
    assert not any("kernel.data" in m for m in rs_modules), (
        f"accessing RegimeState loaded kernel.data (cross-contamination): {rs_modules}"
    )
    assert not any("kernel.exits" in m for m in rs_modules), (
        f"accessing RegimeState loaded kernel.exits (cross-contamination): {rs_modules}"
    )


def test_public_all_exports_resolvable():
    """Every name in __all__ must be resolvable via lazy __getattr__."""
    data = _run_script("""
import json
from renquant_pipeline import public
results = {}
for name in public.__all__:
    obj = getattr(public, name, None)
    results[name] = obj is not None
print(json.dumps(results))
""")
    for name, ok in data.items():
        assert ok, f"public.__all__ lists {name!r} but it resolved to None"
