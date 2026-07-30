"""A fix applied to ONE twin must fail. That is the whole point.

renquant-orchestrator#623 R1: `renquant_pipeline.VetoWeakBuysTask` --- the DOCUMENTED
symbol --- resolves to `panel_scoring.py`, not the kernel, so a kernel-only fix misses it
(pipeline#222). Measured here, that is not a one-off: 9 of 9 public Task exports resolve
outside the kernel and 6 of them have a same-named kernel class.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("twin_pairs", ROOT / "tools" / "twin_pairs.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

PINS = json.loads((ROOT / "twin_pairs.json").read_text())


def test_the_committed_pins_verify_clean():
    assert tp.verify(PINS) == []


def test_the_measured_shape_is_pinned():
    """These three numbers ARE the #623 R1 finding. If any moves, the finding moved."""
    pairs = PINS["pairs"]
    assert len(pairs) == 9
    assert sum(1 for v in pairs.values() if v.get("kernel_twin_file")) == 6
    assert sum(1 for v in pairs.values() if v["public_is_kernel"]) == 0, (
        "a public Task now resolves INTO the kernel — the twin situation changed")


# --- THE regression: one side edited, the other not --------------------------

def test_a_kernel_only_change_is_reported_as_R1():
    """The exact defect. A fix applied to the copy a reader finds first, leaving the
    documented symbol on the old behaviour."""
    pins = copy.deepcopy(PINS)
    name = next(n for n, v in pins["pairs"].items() if v.get("kernel_sha256"))
    pins["pairs"][name]["kernel_sha256"] = "0" * 64
    problems = tp.verify(pins)
    assert len(problems) == 1
    assert "KERNEL twin changed while the public implementation did NOT" in problems[0]
    assert "#623 R1" in problems[0]


def test_a_public_only_change_is_reported():
    pins = copy.deepcopy(PINS)
    name = next(iter(pins["pairs"]))
    pins["pairs"][name]["public_sha256"] = "0" * 64
    problems = tp.verify(pins)
    assert len(problems) == 1
    assert "PUBLIC implementation changed" in problems[0]


def test_both_sides_changing_is_reported_once_and_not_as_R1():
    """Changing both is the CORRECT way to fix a twin, so it must not be reported as
    the one-sided defect --- only as a re-pin prompt."""
    pins = copy.deepcopy(PINS)
    name = next(n for n, v in pins["pairs"].items() if v.get("kernel_sha256"))
    pins["pairs"][name]["public_sha256"] = "0" * 64
    pins["pairs"][name]["kernel_sha256"] = "1" * 64
    problems = tp.verify(pins)
    assert len(problems) == 1
    assert "and so did the kernel twin" in problems[0]
    assert "did NOT" not in problems[0]


def test_a_resolution_change_says_WHICH_COPY_RUNS_may_have_changed():
    pins = copy.deepcopy(PINS)
    name = next(iter(pins["pairs"]))
    pins["pairs"][name]["public_file"] = "renquant_pipeline/kernel/somewhere_else.py"
    problems = tp.verify(pins)
    assert any("WHICH COPY RUNS may have changed" in p for p in problems)


# --- absent / extra pins must not pass silently ------------------------------

def test_an_unpinned_export_is_a_problem():
    pins = copy.deepcopy(PINS)
    name = next(iter(pins["pairs"]))
    del pins["pairs"][name]
    problems = tp.verify(pins)
    assert len(problems) == 1 and "no pin" in problems[0]


def test_a_pin_for_something_no_longer_exported_is_a_problem():
    pins = copy.deepcopy(PINS)
    pins["pairs"]["RetiredTask"] = {"public_module": "x", "public_file": "x",
                                    "public_sha256": "y"}
    problems = tp.verify(pins)
    assert len(problems) == 1 and "no longer a public Task export" in problems[0]


def test_empty_pins_are_a_problem_not_a_pass():
    assert tp.verify({"pairs": {}}) != []
    assert tp.verify({}) != []


# --- helpers -----------------------------------------------------------------

def test_public_task_names_reads_dunder_all_not_dir():
    """`__all__` is the DOCUMENTED surface, and that is what #623 R1 is about. Reading
    dir() would pin internals nobody is told to import.

    Note what is NOT filtered: a leading underscore. Anything listed in `__all__` is
    exported BY DEFINITION, so excluding it would make the pin describe a smaller
    surface than the one callers can import --- the same "checked set is not the real
    set" gap this file exists to close. My first version of this test asserted the
    opposite and was wrong; the code was right.
    """
    class M:
        __all__ = ["BTask", "ATask", "helper", "_HiddenTask"]
    assert tp.public_task_names(M) == ["ATask", "BTask", "_HiddenTask"]
    assert "helper" not in tp.public_task_names(M)


def test_kernel_twin_returns_None_for_a_name_with_no_kernel_class():
    assert tp.kernel_twin("DefinitelyNotAClassNameXYZ") is None


def test_rel_strips_everything_above_src():
    a = tp._rel("/a/b/renquant-pipeline/src/renquant_pipeline/kernel/x.py")
    b = tp._rel("/other/renquant-pipeline-run/src/renquant_pipeline/kernel/x.py")
    assert a == b == "renquant_pipeline/kernel/x.py"


# --- exit codes and the emit contract ----------------------------------------

def test_missing_pin_file_exits_2(tmp_path):
    assert tp.main(["--pins", str(tmp_path / "nope.json")]) == 2


def test_unreadable_pin_file_exits_2(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{truncated")
    assert tp.main(["--pins", str(p)]) == 2


def test_drift_exits_1(tmp_path):
    pins = copy.deepcopy(PINS)
    name = next(iter(pins["pairs"]))
    pins["pairs"][name]["public_sha256"] = "0" * 64
    p = tmp_path / "p.json"
    p.write_text(json.dumps(pins))
    assert tp.main(["--pins", str(p)]) == 1


def test_clean_exits_0():
    assert tp.main([]) == 0


def test_emit_never_writes(tmp_path, capsys):
    p = tmp_path / "p.json"
    p.write_text("SENTINEL")
    assert tp.main(["--emit", "--pins", str(p)]) == 0
    assert p.read_text() == "SENTINEL"
    assert json.loads(capsys.readouterr().out)["pairs"]
