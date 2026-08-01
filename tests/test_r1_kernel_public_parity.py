"""R1's retirement condition #2: a parity test that FAILS when the twins drift.

The twin registry asks for *"a parity test for copies that are meant to agree (R1's
kernel-vs-public, R3's trainers) that fails when they drift, so 'twin' becomes
'mirrored'."*

`tests/test_twin_parity.py` does not cover this: it tests `scripts/check_twin_parity.py`,
which pins **sibling-repo** constants, functions and tax rules — the R0 tripwires. R1's
two copies had nothing.

WHAT IS AND IS NOT ASSERTED HERE. The two modules are **not** meant to be identical: the
public one is deliberately lightweight and does not pull the kernel scoring stack in.
Measured 2026-08-01 — 34 public top-level definitions, 61 kernel, **9 shared names**. So
this pins the surface they DO share, and nothing else:

  * the score-domain constants the public module's own comment calls "in LOCKSTEP";
  * the set of shared symbol names, so a task appearing in one and not the other becomes
    visible instead of silent.

R1's cost was a kernel-only fix that never reached the executing copy
(`renquant-pipeline#222`, three missing guards). A name-level pin cannot catch a
behavioural divergence inside a shared function — that is stated here rather than implied,
because a parity test that reads as stronger than it is would be the same defect one level
up.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "renquant_pipeline"
PUBLIC = SRC / "panel_scoring.py"
KERNEL = SRC / "kernel" / "panel_pipeline" / "job_panel_scoring.py"

#: Constants the public module documents as kept in lockstep with the kernel twin.
LOCKSTEP_CONSTANTS = ("RANK_SCORE_DOMAIN_RAW", "RANK_SCORE_DOMAIN_PROBABILITY")

#: The shared symbol set as measured 2026-08-01. Pinned so that ADDING a name to one copy
#: and not the other is a visible diff here — which is the drift R1 records.
EXPECTED_SHARED = {
    "ApplyGlobalCalibrationTask", "ApplyScoresTask", "BuildFeatureMatrixTask",
    "LoadScorerTask", "PanelScoringJob", "RegimeModelAdmissionTask",
    "VetoWeakBuysTask", "_apply_smalln_guard", "_smalln_guard_params",
}


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _top_level_defs(path: pathlib.Path) -> set[str]:
    return {n.name for n in _tree(path).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _module_constant(path: pathlib.Path, name: str):
    """The literal value of a module-level assignment, or a sentinel if absent.

    Read by AST rather than by importing: importing the kernel module pulls the scoring
    stack in, which is exactly what the public twin exists to avoid, and a parity test
    that forces the heavy import would not run in the environments that need it.
    """
    for node in _tree(path).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    if isinstance(node.value, ast.Constant):
                        return node.value.value
                    return f"<non-literal: {type(node.value).__name__}>"
    return "<absent>"


# --- the lockstep constants -------------------------------------------------

def test_the_LOCKSTEP_constants_agree():
    """The public module's own comment says these are kept in lockstep with the kernel
    twin's. Nothing enforced it."""
    for name in LOCKSTEP_CONSTANTS:
        pub = _module_constant(PUBLIC, name)
        ker = _module_constant(KERNEL, name)
        assert pub == ker, f"{name}: public={pub!r} kernel={ker!r}"


def test_a_lockstep_constant_MISSING_from_either_copy_is_a_failure():
    """`<absent>` must not silently equal `<absent>` — two copies that both dropped a
    constant are not 'in lockstep', they are both broken."""
    for name in LOCKSTEP_CONSTANTS:
        assert _module_constant(PUBLIC, name) != "<absent>", f"{name} gone from public"
        assert _module_constant(KERNEL, name) != "<absent>", f"{name} gone from kernel"


def test_the_comparison_would_DETECT_a_divergence():
    """Anti-vacuity: the helper must be able to return different values, or the test
    above passes on any input."""
    assert _module_constant(PUBLIC, "RANK_SCORE_DOMAIN_RAW") != _module_constant(
        PUBLIC, "RANK_SCORE_DOMAIN_PROBABILITY")
    assert _module_constant(PUBLIC, "NO_SUCH_CONSTANT_ANYWHERE") == "<absent>"


# --- the shared symbol surface ----------------------------------------------

def test_the_SHARED_SYMBOL_SET_is_the_pinned_one():
    """R1's drift is a task existing in one copy and not the other. Pinning the shared
    set makes that a diff here rather than a silence.

    A name added to BOTH copies also fails this — deliberately: the registry's question is
    "which copy executes", and growing the shared surface is exactly when that has to be
    re-answered.
    """
    shared = _top_level_defs(PUBLIC) & _top_level_defs(KERNEL)
    assert shared == EXPECTED_SHARED, {
        "unexpectedly shared": sorted(shared - EXPECTED_SHARED),
        "no longer shared": sorted(EXPECTED_SHARED - shared),
    }


def test_VetoWeakBuysTask_is_in_BOTH_copies():
    """The symbol R1 names, and the one `__init__.py` maps to the public module."""
    assert "VetoWeakBuysTask" in _top_level_defs(PUBLIC)
    assert "VetoWeakBuysTask" in _top_level_defs(KERNEL)


def test_the_smalln_guard_is_shared_and_stays_shared():
    """The small-n CAPITAL guard exists in both. A fix landing in one is the R1 hazard
    with money attached."""
    for name in ("_apply_smalln_guard", "_smalln_guard_params"):
        assert name in _top_level_defs(PUBLIC), name
        assert name in _top_level_defs(KERNEL), name


def test_the_copies_are_NOT_asserted_identical():
    """Stated as a test so the scope cannot drift: they are deliberately different sizes,
    and a parity test that demanded identity would be wrong about the design."""
    pub, ker = _top_level_defs(PUBLIC), _top_level_defs(KERNEL)
    assert pub != ker
    assert len(ker) > len(pub), "the kernel carries the fuller task set by design"


def test_this_pin_is_NAME_LEVEL_and_says_so():
    """A parity test that reads as stronger than it is would be R1's own defect one level
    up: `#222` was a BEHAVIOURAL divergence inside a shared function, which no name-level
    pin can catch."""
    doc = pathlib.Path(__file__).read_text(encoding="utf-8")
    assert "cannot catch a" in doc and "behavioural divergence" in doc
    assert "renquant-pipeline#222" in doc
