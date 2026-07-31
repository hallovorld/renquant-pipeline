"""The inventory tool, exercised against a CONTROLLED fixture tree.

Not against this repository, and not against the operator's other checkouts. A census
of real source is an observation of a moment; a test of it goes red when the source
legitimately changes, and — worse — passes for the wrong reason on a machine that has
none of it. The census lives in `doc/audit/2026-07-31-r7-reverification.md` with its
roots and their HEADs; this file tests the instrument that produced it.

Each fixture below is a way the R7 claim could be wrong: a caller that supplies a real
value, a caller the AST pass would miss, a copy of the package that would double-count.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "call_site_inventory", ROOT / "tools" / "call_site_inventory.py")
INV = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = INV
_spec.loader.exec_module(INV)

FN = "is_wash_sale_blocked_with_cost"
KW = "expected_dollar_return"


def _write(tmp_path, rel, body):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_it_separates_absent_from_explicit_None_from_a_real_value(tmp_path):
    """The whole R7 distinction. "No caller passes it" and "a caller passes None"
    are different facts, and only the first would make the branch dead by omission."""
    _write(tmp_path, "a.py", f"{FN}(ticker='X')\n")
    _write(tmp_path, "b.py", f"{FN}(ticker='X', {KW}=None)\n")
    _write(tmp_path, "c.py", f"{FN}(ticker='X', {KW}=mu_hat)\n")
    got = INV.inventory([tmp_path], FN, KW)
    assert got["counts"] == {INV.ABSENT: 1, INV.EXPLICIT_NONE: 1, INV.REAL_VALUE: 1}
    real = [c for c in got["calls"] if c["kwarg"] == INV.REAL_VALUE]
    assert [c["path"] for c in real] == ["c.py"]


def test_a_real_value_is_found_through_an_attribute_call_and_a_multiline_signature(
        tmp_path):
    """`mod.f(...)` and a call spanning lines are the two shapes a name-only or
    line-oriented scan gets wrong, and both occur at the real call sites."""
    _write(tmp_path, "d.py", f"""
selection.{FN}(
    ticker='X',
    {KW}=compute_mu(),
)
""")
    got = INV.inventory([tmp_path], FN, KW)
    assert got["counts"][INV.REAL_VALUE] == 1, got["calls"]


def test_a_definition_or_an_import_is_not_a_call(tmp_path):
    """Anti-vacuity: if it counted mentions, every fixture above would pass and the
    census would be meaningless."""
    _write(tmp_path, "e.py", f"from k import {FN}\n\n\ndef {FN}(ticker, {KW}=None):\n"
                             f"    return False\n")
    got = INV.inventory([tmp_path], FN, KW)
    assert got["calls"] == [], got["calls"]


def test_a_string_mention_is_REPORTED_rather_than_counted_as_a_call(tmp_path):
    """The AST pass cannot see `getattr(mod, name)(...)`. Reporting zero string
    mentions is what licenses "every call"; reporting some is the warning that it
    does not — so they must be visible, and must not inflate the call count."""
    _write(tmp_path, "f.py", f"fn = getattr(sel, '{FN}')\nfn(ticker='X')\n")
    got = INV.inventory([tmp_path], FN, KW)
    assert got["calls"] == []
    assert [m["path"] for m in got["string_mentions"]] == ["f.py"]


def test_vendored_copies_of_the_package_are_not_counted(tmp_path):
    """An umbrella checkout carries whole snapshots of this package under
    `.subrepo_runtime/` and `artifacts/**/bundle/`. Counting those reports the same
    call site many times and turns a census into a function of how many old bundles
    happen to be on disk."""
    _write(tmp_path, "live/g.py", f"{FN}(ticker='X')\n")
    for vendored in (".subrepo_runtime/repos/p/g.py", "artifacts/run1/bundle/g.py",
                     ".venv/lib/g.py", "worktrees/wt1/g.py"):
        _write(tmp_path, vendored, f"{FN}(ticker='X', {KW}=mu)\n")
    got = INV.inventory([tmp_path], FN, KW)
    assert [c["path"] for c in got["calls"]] == ["live/g.py"]
    assert got["counts"][INV.REAL_VALUE] == 0


def test_an_unparseable_file_is_REPORTED_not_silently_skipped(tmp_path):
    """A scanner that swallows a parse error reports a count that is a lower bound
    while presenting it as a total — the shape of "zero callers" being an artefact of
    the scanner rather than of the code."""
    _write(tmp_path, "broken.py", f"def f(:\n    {FN}(x)\n")
    got = INV.inventory([tmp_path], FN, KW)
    assert [u["path"] for u in got["unparseable"]] == ["broken.py"]


def test_multiple_roots_are_scanned_and_named_in_the_result(tmp_path):
    """The point of the tool: the R7 claim is about consumers, so the census must be
    able to span checkouts — and must record WHICH, because a claim over unnamed
    roots cannot be re-run."""
    a, b = tmp_path / "repo_a", tmp_path / "repo_b"
    _write(a, "x.py", f"{FN}(ticker='X')\n")
    _write(b, "y.py", f"{FN}(ticker='Y', {KW}=mu)\n")
    got = INV.inventory([a, b], FN, KW)
    assert len(got["calls"]) == 2
    assert got["counts"][INV.REAL_VALUE] == 1
    assert sorted(pathlib.Path(r).name for r in got["roots"]) == ["repo_a", "repo_b"]
