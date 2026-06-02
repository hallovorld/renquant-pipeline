"""Phase 5 regression guard — no bare ``kernel.*`` imports survive in subrepo.

Pinned by umbrella ``CLAUDE.md`` §3.5 (one canonical path per business
decision) + the §3.1 PR-based-workflow guarantee that subrepo PRs do not
reintroduce umbrella-bridge imports.

This test is the global, AST-level counterpart to
``test_lift_rewrite_parity.py``, which only covers ``regime`` + ``indicators``.
After Track H Phase 5, the invariant is repo-wide: every ``.py`` file under
``src/renquant_pipeline`` resolves its imports through canonical multi-repo
paths (``renquant_pipeline.kernel.*``, ``renquant_common.*``,
``renquant_artifacts.contracts``, …) — never the umbrella's bare
``kernel.X`` namespace.

If this test ever fails, the bridge has been reintroduced and the subrepo
has lost its standalone-execution guarantee.
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent / "src" / "renquant_pipeline"


def _iter_py_files() -> list[Path]:
    return sorted(
        p for p in PKG_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_no_bare_kernel_imports_anywhere() -> None:
    """Scan every .py under src/renquant_pipeline/ and reject ``kernel.*`` imports.

    Accepts both forms the rewrite eliminated:
      * ``from kernel.X import Y`` (ast.ImportFrom)
      * ``import kernel.X`` (ast.Import)

    The pattern test is intentionally generous — even a future ``kernel``
    submodule name inside renquant-pipeline must not collide with the
    umbrella's top-level ``kernel/`` directory namespace, so a guard at the
    bare-prefix level prevents both bridges AND name collisions.
    """
    offenders: list[str] = []
    for py in _iter_py_files():
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - defensive
            offenders.append(f"{py.relative_to(PKG_ROOT)}: parse error {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Absolute import (level == 0) whose root segment is the
                # umbrella's bare ``kernel`` namespace.
                if node.level == 0 and node.module.split(".", 1)[0] == "kernel":
                    offenders.append(
                        f"{py.relative_to(PKG_ROOT)}:{node.lineno}: "
                        f"from {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(
                            f"{py.relative_to(PKG_ROOT)}:{node.lineno}: "
                            f"import {alias.name}"
                        )
    assert offenders == [], (
        "Phase 5 invariant violated — bare kernel.* imports reintroduced:\n  "
        + "\n  ".join(offenders)
    )


def test_no_artifact_contract_shim_facade() -> None:
    """The ``renquant_pipeline.artifact_contract`` shim was deleted in Phase 5.

    Per §3.5 (one canonical path per business decision), the canonical home
    for artifact contracts is ``renquant_artifacts.contracts``. The shim was
    a duplicate import surface that hid the cross-repo dependency. If anyone
    re-adds it, this test fails — re-route consumers to the canonical path
    instead.
    """
    shim = PKG_ROOT / "artifact_contract.py"
    assert not shim.exists(), (
        f"artifact_contract.py shim reintroduced at {shim} — "
        "import from renquant_artifacts.contracts directly instead "
        "(umbrella CLAUDE.md §3.5)."
    )

    offenders: list[str] = []
    for py in _iter_py_files():
        text = py.read_text(encoding="utf-8")
        if "renquant_pipeline.artifact_contract" in text:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "renquant_pipeline.artifact_contract" in line:
                    offenders.append(f"{py.relative_to(PKG_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "renquant_pipeline.artifact_contract shim referenced:\n  "
        + "\n  ".join(offenders)
    )


def test_no_umbrella_model_contract_imports() -> None:
    """Runtime panel scoring must use the pipeline-local model contract."""
    offenders: list[str] = []
    for py in _iter_py_files():
        text = py.read_text(encoding="utf-8")
        if "training_panel.model_contract" in text:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "training_panel.model_contract" in line:
                    offenders.append(f"{py.relative_to(PKG_ROOT)}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "umbrella training_panel.model_contract imports reintroduced:\n  "
        + "\n  ".join(offenders)
    )
