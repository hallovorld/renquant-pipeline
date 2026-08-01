"""R1's retirement condition #1: the repo itself states which copy executes.

The twin registry asks for *"an executable pointer — the non-live copy raises or logs on
import, or carries a header naming the live one at a path a grep will hit."*

A comment is only a pointer while it is TRUE. These tests bind the two headers to the
actual mapping in `__init__.py`, so re-pointing the public export without updating the
headers fails here instead of silently leaving two files asserting the opposite of what
runs.
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "renquant_pipeline"
PUBLIC = SRC / "panel_scoring.py"
KERNEL = SRC / "kernel" / "panel_pipeline" / "job_panel_scoring.py"
INIT = SRC / "__init__.py"

#: The symbols R1 names. If the registry grows this list, the mapping assertion below
#: covers the new ones automatically.
PUBLIC_SYMBOL = "VetoWeakBuysTask"


def _text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_the_public_export_STILL_maps_to_panel_scoring():
    """The fact both headers assert. If this changes, they become false together."""
    line = next(ln for ln in _text(INIT).splitlines()
                if f'"{PUBLIC_SYMBOL}"' in ln and "(" in ln)
    assert ".panel_scoring" in line, line
    assert "kernel" not in line, line


def test_the_EXECUTING_copy_says_so():
    t = _text(PUBLIC)
    assert "twin registry R1" in t
    assert "what the public export resolves to" in t
    assert "job_panel_scoring.py" in t, "the header must name the other copy by path"


def test_the_NON_EXECUTING_copy_says_so_and_names_the_live_one():
    """The direction that matters: a reader who lands in the kernel file must learn,
    there, that the public symbol does not run it."""
    t = _text(KERNEL)
    assert "twin registry R1" in t
    assert "NOT what the public export resolves to" in t
    assert "panel_scoring.py" in t, "the header must name the other copy by path"


def test_both_headers_name_the_COST_not_just_the_fact():
    """A pointer that says only 'there are two' does not stop the failure. Both name the
    one-directional hazard that has already been paid — renquant-pipeline#222."""
    for p in (PUBLIC, KERNEL):
        assert "renquant-pipeline#222" in _text(p), p.name


def test_the_pointers_are_GREPPABLE_by_the_registry_phrase():
    """The registry's condition is 'at a path a grep will hit'. One phrase finds both."""
    hits = [p.name for p in (PUBLIC, KERNEL) if "twin registry R1" in _text(p)]
    assert sorted(hits) == ["job_panel_scoring.py", "panel_scoring.py"]


def test_this_test_is_NAMED_in_both_headers():
    """So a reader who edits a header is told what will catch them, and a reader who
    breaks this test knows which headers to fix."""
    for p in (PUBLIC, KERNEL):
        assert "test_r1_executable_pointer.py" in _text(p), p.name
