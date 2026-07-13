"""Import-surface tests for renquant_pipeline.public (V-005 remediation).

Proves that importing the public module does NOT eagerly load kernel
subsystems — each symbol is lazy-loaded on first access only, and the
``load_universe`` OPERATION's kernel import is function-scoped (loaded only
when called, not when referenced or imported).

The import-isolation checks run in a fresh subprocess so parent-process
module cache cannot mask real violations (codex review on this PR).
``load_universe``'s functional-correctness tests run in-process (tmp_path
fixtures) — they are consumer-contract tests, not import-surface proofs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


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
    assert result.returncode == 0, (
        f"subprocess exited {result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, (
        f"subprocess produced no JSON output:\nstderr: {result.stderr}"
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


def test_load_universe_import_is_lazy_until_called():
    """``load_universe``'s kernel import is function-scoped: importing
    ``renquant_pipeline.public`` or merely referencing the ``load_universe``
    function object must NOT load ``kernel.pipeline.job_universe``; only
    CALLING it may. Calling it against a strategy_dir with no ``models/``
    directory exercises the real chain (LoadArtifactsTask short-circuits)
    without needing artifact fixtures inside the subprocess."""
    data = _run_script("""
import json, sys, tempfile
import renquant_pipeline
import renquant_pipeline.public as public

before_import = set(sys.modules)
_ = public.load_universe  # referencing the function object only
after_ref = set(sys.modules) - before_import

tmp_dir = tempfile.mkdtemp()
before_call = set(sys.modules)
result = public.load_universe(config={}, strategy_dir=tmp_dir)
after_call = set(sys.modules) - before_call

print(json.dumps({
    "after_ref": sorted(n for n in after_ref if "kernel" in n),
    "after_call": sorted(n for n in after_call if "kernel" in n),
    "models": result.models,
    "rejections": result.rejections,
}))
""")
    assert data["after_ref"] == [], (
        f"merely referencing load_universe loaded kernel modules "
        f"(should be function-scoped): {data['after_ref']}"
    )
    assert any("kernel.pipeline.job_universe" in m for m in data["after_call"]), (
        f"calling load_universe did not load kernel.pipeline.job_universe: "
        f"{data['after_call']}"
    )
    assert data["models"] == {}
    assert data["rejections"] == []


def test_load_universe_admits_valid_artifact_and_reports_rejection(tmp_path):
    """Consumer-contract test: ``load_universe`` runs the real
    ``LoadUniverseJob`` chain and returns a ``UniverseLoadResult`` with the
    admitted model plus the rejection reason for a ticker with no artifact —
    faithful to what ``native_context_hydration.py`` did by constructing
    ``LoadUniverseJob``/``UniverseContext`` directly (pipeline#197 round 1,
    point 2 / orchestrator#513)."""
    from renquant_pipeline.public import UniverseLoadResult, load_universe

    models_dir = tmp_path / "models"
    aaa_dir = models_dir / "AAA"
    aaa_dir.mkdir(parents=True)
    (aaa_dir / "AAA-policy-metadata.json").write_text(json.dumps({
        "policy_type": "manual",
        "feature_columns": [],
    }))
    (aaa_dir / "AAA-manual-rules.json").write_text(json.dumps({
        "score_rules": [],
        "buy_threshold": 0.1,
        "sell_threshold": -0.1,
    }))
    # BBB has a directory but no policy-metadata file -> load_artifact
    # returns None -> LoadArtifactsTask records a "no_artifact" rejection.
    (models_dir / "BBB").mkdir(parents=True)

    result = load_universe(
        config={"watchlist": ["AAA", "BBB"]},
        strategy_dir=tmp_path,
    )

    assert isinstance(result, UniverseLoadResult)
    assert "AAA" in result.models
    assert result.models["AAA"]["policy_type"] == "manual"
    assert ("BBB", "no_artifact") in result.rejections


def test_load_universe_held_tickers_is_authoritative_over_state_file(tmp_path):
    """``held_tickers``, when given (even an empty set), is AUTHORITATIVE —
    it wins over state-file-derived holdings rather than being treated as
    "unset" (matching ``UniverseContext``'s own held/``None`` distinction).
    A ticker with a sub-floor quality metric is exempted from the
    universe-floor filter when passed as held, and dropped when it is not
    (empty held set) — proving the set value, not just its truthiness, is
    threaded through."""
    from renquant_pipeline.public import load_universe

    aaa_dir = tmp_path / "models" / "AAA"
    aaa_dir.mkdir(parents=True)
    (aaa_dir / "AAA-policy-metadata.json").write_text(json.dumps({
        "policy_type": "manual",
        "sharpe": 0.1,  # below the floor threshold configured below
    }))
    (aaa_dir / "AAA-manual-rules.json").write_text(json.dumps({
        "score_rules": [],
        "buy_threshold": 0.1,
        "sell_threshold": -0.1,
    }))
    config = {
        "watchlist": ["AAA"],
        "ranking": {"universe_floor": {"type": "sharpe", "threshold": 1.0}},
    }

    not_held = load_universe(
        config=config, strategy_dir=tmp_path, held_tickers=set(),
    )
    assert "AAA" not in not_held.models
    assert any(ticker == "AAA" for ticker, _reason in not_held.rejections)

    held = load_universe(
        config=config, strategy_dir=tmp_path, held_tickers={"AAA"},
    )
    assert "AAA" in held.models
    assert held.rejections == []
