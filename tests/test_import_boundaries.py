"""Import-boundary tests.

Per RFC §"Cross-Repo Contracts → Boundary test matrix" and §"Forbidden
dependencies", ``renquant-pipeline`` must NOT import model backends or
model-family packages — scorers reach it only through
``renquant_common.load_scorer`` + entry points.

The runtime check (`test_pipeline_import_does_not_pull_training_or_execution`)
catches eager imports; the AST scan
(`test_pipeline_source_does_not_reference_forbidden_modules`) also catches
lazy / guarded imports buried inside functions.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

PIPELINE_SRC = Path(__file__).parent.parent / "src" / "renquant_pipeline"

FORBIDDEN_ROOT_IMPORTS = (
    "alpaca",
    "ib_insync",
    "live",
    "renquant_execution",
    "renquant_model_gbdt",
    "renquant_model_patchtst",
    "renquant_model",  # post-P3 merged repo
    "torch",
    "transformers",
    "xgboost",
    "lightgbm",
    "catboost",
)


def test_pipeline_import_does_not_pull_training_or_execution() -> None:
    """Runtime check — module-level imports do not pull forbidden roots."""
    before = set(sys.modules)
    importlib.import_module("renquant_pipeline")
    imported = set(sys.modules) - before
    offenders = sorted(
        name for name in imported
        if name in FORBIDDEN_ROOT_IMPORTS or name.startswith(FORBIDDEN_ROOT_IMPORTS)
    )
    assert offenders == [], (
        "renquant-pipeline must not import model-family or execution packages "
        "at runtime — scorers go through renquant_common.load_scorer + "
        "entry points (RFC §'Cross-Repo Contracts → Scorer Protocol')."
    )


def _root(module_name: str) -> str:
    return module_name.split(".", 1)[0]


def _collect_imports(tree: ast.AST) -> set[str]:
    """Return the set of imported root module names anywhere in the AST."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(_root(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None and node.level == 0:
                roots.add(_root(node.module))
    return roots


#: Phase 1 byte-equivalent lift zones (C2.9, C2.11). These files were copied
#: byte-for-byte from umbrella kernel/ and DO import torch/xgboost/alpaca
#: directly — that's what the original umbrella code does. The RFC's
#: ideal is for these to move into renquant-model; the rewrite is a
#: planned Phase 5+ step, not a Phase 1 invariant. Until then the boundary
#: check excludes these files specifically so the lifts can land cleanly.
_PHASE1_EXCLUSIONS = {
    # C2.9 — data.py imports alpaca (broker fallback for OHLCV fetch)
    "kernel/data.py",
    # C2.11 — panel_pipeline scorers import torch/xgboost/transformers because
    # the umbrella code itself does. The proper resting place per RFC is
    # renquant-model (which would invert the dep and have pipeline consume
    # scorers via renquant_common.load_scorer). Phase 5+ work.
    "kernel/panel_pipeline/panel_scorer.py",
    "kernel/panel_pipeline/patchtst_scorer.py",
    "kernel/panel_pipeline/hf_patchtst_scorer.py",
}


def test_pipeline_source_does_not_reference_forbidden_modules() -> None:
    """Static check — no .py file in src/ even mentions a forbidden import.

    Catches lazy imports inside functions that the runtime check misses
    (e.g., a `import xgboost` deferred under `def predict_rows`).

    Phase 1 byte-equivalent lift zones (see ``_PHASE1_EXCLUSIONS``) are
    excluded — those files are 1:1 copies of umbrella code pending the
    Phase 5+ move to renquant-model.
    """
    offenders: list[tuple[Path, str]] = []
    for py in PIPELINE_SRC.rglob("*.py"):
        rel = py.relative_to(PIPELINE_SRC)
        if str(rel).replace("\\", "/") in _PHASE1_EXCLUSIONS:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        roots = _collect_imports(tree)
        bad = roots & set(FORBIDDEN_ROOT_IMPORTS)
        for root in sorted(bad):
            offenders.append((rel, root))
    assert offenders == [], (
        f"renquant-pipeline source references forbidden imports: {offenders}. "
        f"Move backend-specific code into the corresponding renquant-model "
        f"subdir and register an entry point; pipeline must consume scorers "
        f"only via renquant_common.load_scorer (RFC §'Bootstrap Drift Audit' "
        f"item 1). Phase 1 byte-equivalent excluded files: {sorted(_PHASE1_EXCLUSIONS)}."
    )


def test_pipeline_uses_RegimeLabel_not_raw_strings() -> None:
    """Static check — context.py imports RegimeLabel (regime taxonomy single
    source of truth per RFC §'Cross-Repo Contracts → RegimeLabel')."""
    text = (PIPELINE_SRC / "context.py").read_text(encoding="utf-8")
    assert "from renquant_common import RegimeLabel" in text or \
           "from renquant_common.contracts.regime import RegimeLabel" in text, (
        "context.py must import RegimeLabel from renquant_common"
    )
