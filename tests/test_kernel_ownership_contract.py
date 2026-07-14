"""Kernel-ownership contract tests (G3 F-8).

``renquant_pipeline.kernel.NON_OWNED_KERNEL_STEMS`` is the pinned, versioned
declaration that ``renquant-orchestrator``'s ``bootstrap_multirepo`` (and any
other consumer bootstrapping against a pinned pipeline checkout) reads to
decide whether an import failure for a kernel-directory entry is a tolerated
exception or a hard, fail-closed error (PR #514 round 1 / orchestrator
``live_bridge.py``).

These tests pin the pipeline side of that contract:

1. Every entry physically present in ``kernel/`` that is NOT declared in
   ``NON_OWNED_KERNEL_STEMS`` must import cleanly — this is what makes the
   frozenset a real, enforced contract rather than aspirational prose.
2. ``NON_OWNED_KERNEL_STEMS`` itself is immutable and every declared stem
   still physically exists (catches stale declarations left behind after a
   module is deleted or renamed).
"""
from __future__ import annotations

import importlib
from pathlib import Path

from renquant_pipeline import kernel as pipeline_kernel

KERNEL_DIR = Path(pipeline_kernel.__file__).resolve().parent


def _kernel_stems() -> list[str]:
    stems: list[str] = []
    for entry in sorted(KERNEL_DIR.iterdir()):
        stem = entry.stem if entry.suffix == ".py" else entry.name
        if stem in {"__init__", "__pycache__"} or stem.startswith("."):
            continue
        if entry.suffix not in {".py", ""}:
            continue
        stems.append(stem)
    return stems


def test_non_owned_kernel_stems_is_frozen() -> None:
    assert isinstance(pipeline_kernel.NON_OWNED_KERNEL_STEMS, frozenset)


def test_non_owned_kernel_stems_still_exist() -> None:
    """Guards against a stale exemption for a module that was since removed
    or renamed — an exemption for a stem that no longer exists is dead
    prose, not an enforced contract."""
    present = set(_kernel_stems())
    stale = pipeline_kernel.NON_OWNED_KERNEL_STEMS - present
    assert stale == set(), (
        f"NON_OWNED_KERNEL_STEMS declares stem(s) no longer present in "
        f"kernel/: {sorted(stale)}. Remove the stale exemption."
    )


def test_all_declared_owned_kernel_stems_import_cleanly() -> None:
    """The actual enforced contract: every kernel-directory entry NOT
    declared in NON_OWNED_KERNEL_STEMS must import without error. A failure
    here means this package would break renquant-orchestrator's fail-closed
    bootstrap for a module pipeline claims to own — fix the module, or if it
    is genuinely no longer pipeline-owned, get that change reviewed and add
    it to NON_OWNED_KERNEL_STEMS with a documented reason (see the kernel
    package docstring)."""
    owned = [s for s in _kernel_stems() if s not in pipeline_kernel.NON_OWNED_KERNEL_STEMS]
    failures: list[str] = []
    for stem in owned:
        try:
            importlib.import_module(f"renquant_pipeline.kernel.{stem}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{stem}: {type(exc).__name__}: {exc}")
    assert failures == [], (
        "pinned kernel-ownership contract violated — these modules are NOT "
        "declared in NON_OWNED_KERNEL_STEMS and must import cleanly:\n"
        + "\n".join(failures)
    )
