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


# The ACTIVE ledger contract's params block, verbatim from the committed
# genesis artifact (artifacts/momentum/2026-08-02/momentum_residual_v0.json,
# RenQuant umbrella). The s104 blend profiles pin the fp of exactly these.
_LIVE_V0_PARAMS = {
    "min_features": 3,
    "min_obs": 200,
    "min_side_obs": 30,
    "names_per_date_floor": 50,
    "params_source": (
        "tools/goal7_momentum_run.py::FROZEN + MIN_SIDE_OBS (frozen in "
        "model#164 §2, F5 floor in model#177); mirrored into "
        "renquant_model_momentum._frozen_params_v0 so the wheel is "
        "self-sufficient, with equality held by "
        "test_params_v0_mirrors_the_sealed_v1_runner"
    ),
    "params_version": "v0",
    "skip": 21,
    "window": 252,
}


def test_known_vector_stability():
    """The recipe is a PUBLISHED contract (codex on pipeline#266): the alias-
    equality test cannot catch a recipe change because both paths import this
    one function — only a pinned LITERAL can. This is the active ledger
    contract's fingerprint; changing canonicalization/version/hash is an
    explicit compatibility decision that must rewrite this literal knowingly
    (and rotate every profile pin with it)."""
    from renquant_pipeline.momentum_identity import params_fingerprint
    assert params_fingerprint(_LIVE_V0_PARAMS) == "momentum-v0-fd65161a20b29314"
    # order-free canonicalization on the same vector
    assert params_fingerprint(
        dict(sorted(_LIVE_V0_PARAMS.items(), reverse=True))
    ) == "momentum-v0-fd65161a20b29314"
