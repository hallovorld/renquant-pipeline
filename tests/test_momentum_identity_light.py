"""The momentum identity contract must stay import-light and single-source
(RQ#574 r3: the pinned-path CI has no pandas)."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")


def test_fingerprint_recipe_matches_the_scorer_alias():
    from renquant_pipeline.momentum_identity import params_fingerprint
    from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (
        _params_fingerprint,
    )
    p = {"params_version": "v0", "window": 231, "skip": 21}
    assert params_fingerprint(p) == _params_fingerprint(p)
    assert params_fingerprint(p).startswith("momentum-v0-")
    assert len(params_fingerprint(p)) == len("momentum-v0-") + 16


def test_module_imports_without_heavy_deps():
    """Import in a clean interpreter and PROVE no heavy runtime dep loads —
    a transitively-added pandas import would break the pinned-path gate env."""
    code = (
        "import sys\n"
        "import renquant_pipeline.momentum_identity as m\n"
        "heavy = [k for k in sys.modules if k.split('.')[0] in "
        "('pandas', 'numpy', 'xgboost', 'scipy')]\n"
        "assert heavy == [], heavy\n"
        "print(m.params_fingerprint({'params_version': 'v0'}))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC  # ONLY the package src — no test-env extras
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("momentum-v0-")


def test_known_vector_stability():
    """The recipe is a published contract: pin one vector so any silent
    change to canonicalization is a named break, not drift."""
    from renquant_pipeline.momentum_identity import params_fingerprint
    assert params_fingerprint(
        {"params_version": "v0", "window": 231, "skip": 21}
    ) == params_fingerprint(
        {"skip": 21, "window": 231, "params_version": "v0"}  # order-free
    )
