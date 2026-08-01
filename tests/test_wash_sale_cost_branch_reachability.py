"""GOAL-3 #623 row R7 — does any call site in this repo's src/ reach the branch?

The registry recorded: *"`is_wash_sale_blocked_with_cost` branch (a) validates nothing
in production — no caller passes `expected_dollar_return`"*, measured at **3** call
sites.

Re-measured 2026-07-31: there are now **6** call sites in `src/`, and **still zero**
pass a real `expected_dollar_return`. The one site that names the parameter passes
`None` explicitly, with the comment *"μ̂ not yet known at this stage"*.

So the row is stale in the direction of UNDERSTATING it: the call sites doubled while
the defect persisted. That is the registry-rot this file exists to stop — the count is
now asserted, so the next caller added without the parameter fails a test instead of
quietly extending an unreached branch.

SCOPE, corrected after review. This file measures THIS REPOSITORY'S `src/` and nothing
else, so it cannot support "the branch never executes in production": `renquant_pipeline`
is a shared package and a consumer could supply the parameter. The cross-repo census
that closes that gap is in `doc/audit/2026-07-31-r7-reverification.md` (zero calls in
any repo outside this one; the umbrella's six are its vendored copy of this kernel), and
`tools/call_site_inventory.py` makes it rerunnable. It is deliberately NOT asserted here
— it is a fact about ten checkouts on one machine, which is an observation to date, not
a property of this repository.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "renquant_pipeline"
FN = "is_wash_sale_blocked_with_cost"


def _call_sites():
    """Every CALL of the function in src/, by AST — not by grep.

    A line-oriented regex over a multi-line call signature is how this repo has
    published wrong counts before; the call spans several lines at every site.
    """
    out = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == FN:
                    kw = {k.arg: k.value for k in node.keywords if k.arg}
                    out.append((str(path.relative_to(SRC)), node.lineno, kw))
    return out


def test_the_call_site_count_is_asserted_not_remembered():
    sites = _call_sites()
    assert len(sites) == 6, [(p, l) for p, l, _ in sites]


def test_zero_call_sites_pass_a_real_expected_dollar_return():
    """THE R7 finding, still true: branch (a) is not reached from any call site in
    `src/`. Not "never executes in production" — see this module's docstring."""
    passing = []
    for path, line, kw in _call_sites():
        node = kw.get("expected_dollar_return")
        if node is None:
            continue                                    # parameter not supplied
        if isinstance(node, ast.Constant) and node.value is None:
            continue                                    # supplied, explicitly None
        passing.append((path, line))
    assert passing == [], passing


def test_exactly_one_site_names_the_parameter_and_passes_None():
    """Anti-vacuity: if NO site mentioned it, the test above would pass trivially
    even after someone deleted the parameter entirely."""
    named = [(p, l, kw) for p, l, kw in _call_sites() if "expected_dollar_return" in kw]
    assert len(named) == 1, named
    node = named[0][2]["expected_dollar_return"]
    assert isinstance(node, ast.Constant) and node.value is None


def test_the_function_still_has_the_cost_aware_branch():
    """If branch (a) is ever removed, this file's premise changes and it must be
    rewritten rather than left asserting a shape that no longer exists."""
    src = (SRC / "kernel" / "selection.py").read_text(encoding="utf-8")
    assert "if expected_dollar_return is None:" in src
    assert "safety_margin * cost_npv" in src
