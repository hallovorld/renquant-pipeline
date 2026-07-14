"""Kernel-ownership contract tests (G3 F-8).

``renquant_pipeline.kernel.NON_OWNED_KERNEL_STEMS`` is the pinned, versioned
declaration that ``renquant-orchestrator``'s ``bootstrap_multirepo`` (and any
other consumer bootstrapping against a pinned pipeline checkout) reads to
decide whether an import failure for a kernel-directory entry is a tolerated
exception or a hard, fail-closed error (PR #514 round 1 / orchestrator
``live_bridge.py``).

``renquant_pipeline.kernel.OWNED_KERNEL_STEMS`` is the companion, positive
declaration (PR #514 round 4 / pipeline PR #198): every stem this package
guarantees to ship. ``bootstrap_multirepo`` uses it as a path-identity /
sanity check on the pipeline checkout it discovered, replacing an earlier
arbitrary orchestrator-local minimum-module-count heuristic Codex flagged as
not tied to any real pinned contract.

These tests pin the pipeline side of that contract:

1. Every entry physically present in ``kernel/`` that is NOT declared in
   ``NON_OWNED_KERNEL_STEMS`` must import cleanly — this is what makes the
   frozenset a real, enforced contract rather than aspirational prose.
2. ``NON_OWNED_KERNEL_STEMS`` and ``OWNED_KERNEL_STEMS`` are each immutable,
   and every declared stem in either one still physically exists (catches
   stale declarations left behind after a module is deleted or renamed).
3. ``OWNED_KERNEL_STEMS`` and ``NON_OWNED_KERNEL_STEMS`` are disjoint, and
   their union exactly equals what is physically present in ``kernel/`` —
   this is what makes ``OWNED_KERNEL_STEMS`` a real, structurally-enforced
   inventory rather than a hand-maintained list that silently drifts: a
   kernel module added, removed, or renamed without a matching update to one
   of these two declarations fails this test.
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


def test_owned_kernel_stems_is_frozen() -> None:
    assert isinstance(pipeline_kernel.OWNED_KERNEL_STEMS, frozenset)


def test_owned_kernel_stems_still_exist() -> None:
    """Guards against a stale OWNED_KERNEL_STEMS entry for a module that was
    since removed or renamed — mirrors
    ``test_non_owned_kernel_stems_still_exist`` for the positive side of the
    contract."""
    present = set(_kernel_stems())
    stale = pipeline_kernel.OWNED_KERNEL_STEMS - present
    assert stale == set(), (
        f"OWNED_KERNEL_STEMS declares stem(s) no longer present in kernel/: "
        f"{sorted(stale)}. Remove the stale declaration."
    )


def test_owned_and_non_owned_kernel_stems_are_disjoint() -> None:
    """A stem is either pipeline-owned or explicitly exempted, never both —
    otherwise a consumer reading both declarations (renquant-orchestrator's
    ``bootstrap_multirepo``) would see contradictory guidance for the same
    stem."""
    overlap = pipeline_kernel.OWNED_KERNEL_STEMS & pipeline_kernel.NON_OWNED_KERNEL_STEMS
    assert overlap == set(), (
        f"stem(s) declared in both OWNED_KERNEL_STEMS and "
        f"NON_OWNED_KERNEL_STEMS: {sorted(overlap)}."
    )


def test_declared_kernel_stems_match_directory_contents() -> None:
    """The real structural guarantee behind this contract: the union of
    OWNED_KERNEL_STEMS and NON_OWNED_KERNEL_STEMS must equal exactly what is
    physically present in kernel/ — this is what makes
    renquant-orchestrator's ``bootstrap_multirepo`` path-identity check
    (comparing its discovered directory against the pinned
    OWNED_KERNEL_STEMS) meaningful rather than aspirational: if this
    declaration could silently drift from the real directory contents, a
    consumer comparing against it would never actually catch a wrong/empty
    checkout, because the pinned "expected" inventory could just as easily
    be wrong too. A kernel module added or removed without updating one of
    the two declarations fails this test."""
    present = set(_kernel_stems())
    declared = pipeline_kernel.OWNED_KERNEL_STEMS | pipeline_kernel.NON_OWNED_KERNEL_STEMS
    undeclared = present - declared
    assert undeclared == set(), (
        f"kernel/ contains stem(s) not declared in either OWNED_KERNEL_STEMS "
        f"or NON_OWNED_KERNEL_STEMS: {sorted(undeclared)}. Add new kernel "
        f"modules to OWNED_KERNEL_STEMS (or, if genuinely not "
        f"pipeline-owned, to NON_OWNED_KERNEL_STEMS with a documented "
        f"reason)."
    )
    stale = declared - present
    assert stale == set(), (
        f"OWNED_KERNEL_STEMS/NON_OWNED_KERNEL_STEMS declare stem(s) no "
        f"longer present in kernel/: {sorted(stale)}. Remove the stale "
        f"declaration(s)."
    )
