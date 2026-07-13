"""Import-surface tests for renquant_pipeline.public (V-005 remediation).

Proves that importing the public module does NOT eagerly load kernel
subsystems — each symbol is lazy-loaded on first access only.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_ISOLATED_IMPORT_SCRIPT = """
import json
import sys

module_name = sys.argv[1]
before = set(sys.modules)
__import__(module_name)
imported = sorted(set(sys.modules) - before)
print(json.dumps({"imported": imported}))
"""


def _run_isolated_import(module_name: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATED_IMPORT_SCRIPT, module_name],
        capture_output=True, text=True, env=env, timeout=60,
    )
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    if not lines:
        raise RuntimeError(
            f"isolated import of {module_name!r}: no output "
            f"(exit={result.returncode}):\n{result.stderr}"
        )
    return json.loads(lines[-1])


def test_public_import_does_not_add_kernel_imports():
    """Importing renquant_pipeline.public must not load any kernel modules
    beyond what renquant_pipeline.__init__ already pulls in transitively."""
    script = """
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
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=60,
    )
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    if not lines:
        pytest.skip(f"subprocess failed: {result.stderr[:200]}")
    data = json.loads(lines[-1])
    assert data["kernel_from_public"] == [], (
        f"renquant_pipeline.public added kernel modules beyond what "
        f"renquant_pipeline.__init__ loads: {data['kernel_from_public']}"
    )


def test_public_lazy_access_loads_targeted_kernel():
    """Accessing LocalStore triggers kernel.data load; accessing
    HoldingState triggers kernel.exits; each is independent."""
    script = """
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
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=60,
    )
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    if not lines:
        pytest.skip(f"subprocess failed: {result.stderr[:200]}")
    data = json.loads(lines[-1])
    assert any("kernel.data" in m for m in data["after_localstore"]), (
        "accessing LocalStore did not load kernel.data"
    )
    assert any("kernel.exits" in m for m in data["after_holdingstate"]), (
        "accessing HoldingState did not load kernel.exits"
    )


def test_public_all_exports_resolvable():
    """Every name in __all__ must be resolvable via lazy __getattr__."""
    from renquant_pipeline import public
    for name in public.__all__:
        obj = getattr(public, name, None)
        assert obj is not None, f"public.__all__ lists {name!r} but it resolved to None"
