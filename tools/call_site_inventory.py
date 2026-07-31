#!/usr/bin/env python3
"""Inventory every CALL of a function across one or more checkouts. (GOAL-3, R7)

The claim this exists to support is a scope claim: *"no caller passes
``expected_dollar_return``, so the cost-aware branch never executes."* A scan of this
repository's ``src/`` cannot establish that — ``renquant_pipeline`` is a shared package,
and a call from a consumer repo would be invisible to it. So the inventory takes ROOTS,
prints what it looked at, and the document quotes both the result AND the roots.

What it can see, and what it cannot:

* **calls by name** — ``f(...)``, ``mod.f(...)``, at any nesting depth, by AST rather
  than by regex, because every call site here spans several lines and this programme
  has published wrong counts from line-oriented greps before;
* **the kwarg's shape** — absent / explicitly ``None`` / a real expression, which is the
  distinction the R7 claim rests on;
* **string mentions**, reported separately, because a name reachable through
  ``getattr``/a dispatch table would be invisible to the AST pass. Reporting zero of
  those is what lets the AST count stand for "every call"; reporting some is a warning
  that it does not.

It CANNOT see a consumer outside the roots it was given — an installed wheel, a private
fork, a checkout on another machine. That limit is a property of static scanning, not a
choice, and the honest form of the claim names its roots.

Usage:
    python3 tools/call_site_inventory.py --function is_wash_sale_blocked_with_cost \\
        --kwarg expected_dollar_return --root . [--root ../renquant-orchestrator] [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

#: Directories that are copies, caches or build output rather than source anyone runs.
#: `.subrepo_runtime` and `artifacts/**/bundle` in particular hold whole SNAPSHOTS of
#: this package; counting them would report the same call dozens of times and inflate
#: any census taken over an umbrella checkout.
SKIP_DIRS = frozenset({
    ".git", ".venv", "site-packages", "node_modules", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".claude", "worktrees",
    ".subrepo_runtime", "artifacts", "bundle",
})

ABSENT = "absent"
EXPLICIT_NONE = "explicit_none"
REAL_VALUE = "real_value"


def _kwarg_kind(node: ast.Call, kwarg: str) -> str:
    for kw in node.keywords:
        if kw.arg != kwarg:
            continue
        if isinstance(kw.value, ast.Constant) and kw.value.value is None:
            return EXPLICIT_NONE
        return REAL_VALUE
    return ABSENT


def inventory(roots, function: str, kwarg: str) -> dict:
    calls, string_mentions, unparseable = [], [], []
    for root in roots:
        root = pathlib.Path(root).resolve()
        for path in sorted(root.rglob("*.py")):
            if SKIP_DIRS & set(path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if function not in text:
                continue
            rel = str(path.relative_to(root))
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                unparseable.append({"root": str(root), "path": rel, "error": str(exc)})
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == function:
                        calls.append({"root": str(root), "path": rel, "line": node.lineno,
                                      "kwarg": _kwarg_kind(node, kwarg)})
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and function in node.value:
                    string_mentions.append({"root": str(root), "path": rel,
                                            "line": node.lineno})
    return {
        "function": function, "kwarg": kwarg,
        "roots": [str(pathlib.Path(r).resolve()) for r in roots],
        "calls": calls,
        "counts": {k: sum(1 for c in calls if c["kwarg"] == k)
                   for k in (ABSENT, EXPLICIT_NONE, REAL_VALUE)},
        "string_mentions": string_mentions,
        "unparseable": unparseable,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--function", required=True)
    ap.add_argument("--kwarg", required=True)
    ap.add_argument("--root", action="append", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = inventory(args.root, args.function, args.kwarg)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    c = result["counts"]
    print(f"{args.function}({args.kwarg}=…) over {len(result['roots'])} root(s):")
    for r in result["roots"]:
        print(f"  root: {r}")
    print(f"  calls: {len(result['calls'])}  "
          f"[{REAL_VALUE}={c[REAL_VALUE]}  {EXPLICIT_NONE}={c[EXPLICIT_NONE]}  "
          f"{ABSENT}={c[ABSENT]}]")
    for call in result["calls"]:
        print(f"    {call['path']}:{call['line']}  {call['kwarg']}")
    if result["string_mentions"]:
        print(f"  !! {len(result['string_mentions'])} string mention(s) — the AST count "
              f"does not cover dynamic dispatch; check each:")
        for m in result["string_mentions"]:
            print(f"    {m['path']}:{m['line']}")
    if result["unparseable"]:
        print(f"  !! {len(result['unparseable'])} file(s) failed to parse and were NOT "
              f"scanned — the count is a lower bound:")
        for u in result["unparseable"]:
            print(f"    {u['path']}: {u['error']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
