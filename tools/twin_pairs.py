#!/usr/bin/env python3
"""Pin every public export and its same-named kernel twin. (GOAL-3, orch#623 R1)

The orchestrator's twin registry (renquant-orchestrator#623) names the defect: *"the
failure is not that duplicates exist — some duplication is deliberate. The failure is
that nothing in the repo tells you which copy executes."* Its row R1 is this package:
``renquant_pipeline.VetoWeakBuysTask``, the **documented** symbol, resolves to
``panel_scoring.py`` and not to the kernel, so a kernel-only fix misses it. That cost a
real defect (pipeline#222).

Measured here, R1 is not a one-off:

* **19** public exports have a same-named definition under ``kernel/``;
* the previous ``*Task``-only scope covered **6** of them.

So six documented symbols each have a twin, and nothing mechanically relates them.

**What this pins, and why it is the pair rather than the file.** For each pair, the
sha256 of *each side's* class source. Editing one side without the other changes one
digest and fails the check. That is precisely the defect: a fix applied to the copy a
reader finds first, while the copy the documented symbol resolves to keeps the old
behaviour. Re-pinning requires ``--emit``, which forces the author to look at both.

It is deliberately **noisy in the safe direction**: a comment-only edit also trips it.
Cheap to re-pin, and the alternative — normalising the source until only "real" changes
count — is a second implementation of "what counts as a change", which is the same class
of defect this file exists to catch.

``--emit`` prints; it never writes. The pin file changes only through a reviewed PR.

Exit codes: ``0`` pins hold, ``1`` drift, ``2`` usage/IO error.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import pathlib
import re
import sys
from typing import Any

PINS = pathlib.Path(__file__).resolve().parent.parent / "twin_pairs.json"
KERNEL_DIR = (pathlib.Path(__file__).resolve().parent.parent
              / "src" / "renquant_pipeline" / "kernel")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(path: str | None) -> str | None:
    if not path:
        return None
    parts = pathlib.Path(path).resolve().parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "src" and i + 1 < len(parts):
            return "/".join(parts[i + 1:])
    return "/".join(parts[-3:])


def public_export_names(module) -> list[str]:
    """EVERY public export, read off ``__all__``. No name filter.

    ``__all__`` and not ``dir()``: the documented surface is the one a caller is told to
    import, and it is the surface #623 R1 is about.

    THE FILTER THIS FUNCTION USED TO HAVE. It selected ``n.endswith("Task")``. That is an
    enumerated scope, and an enumerated scope passes forever for everything outside it.
    Measured 2026-07-31: **19** public exports have a same-named definition under
    ``kernel/``; the ``*Task`` filter covered **6**. The 13 it silently excluded include
    ``stamp_order_attribution`` and ``validate_order_attribution`` -- order-attribution
    functions on the capital path -- plus ``PanelScoringJob`` and ``SelectionJob``, which
    are Jobs and so never matched a suffix looking for Tasks.

    The default is now inverted: scan everything, and let ``kernel_twin`` decide. A tool
    built to find the copy that runs must not have a scope narrower than the class of
    defect it is looking for.
    """
    return sorted(getattr(module, "__all__", []))


def kernel_twin(name: str) -> str | None:
    """Path of a same-named class OR function under ``kernel/``, or None.

    THE SECOND ENUMERATED SCOPE. This matched ``^class NAME`` only, so a function twin
    was invisible even to a caller that had already stopped filtering on ``*Task``. Two
    narrow scopes stacked: the first excluded the names, the second excluded the kinds.
    """
    pattern = re.compile(
        rf"^(?:class|def|async def) {re.escape(name)}\b", re.M)
    for path in sorted(KERNEL_DIR.rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            return _rel(str(path))
    return None


def survey() -> dict[str, Any]:
    import renquant_pipeline as rp

    pairs: dict[str, Any] = {}
    for name in public_export_names(rp):
        obj = getattr(rp, name)
        try:
            source_file = inspect.getsourcefile(obj)
        except TypeError:
            # Not a source object (a constant, a re-exported value). It is RECORDED,
            # not skipped: silently dropping names from `__all__` would put them
            # outside the scan the same way the `*Task` filter did, and the invariant
            # that every documented export appears in this file is what lets a reader
            # trust the absence of a warning.
            pairs[name] = {
                "kind": "not-a-source-object",
                "public_type": type(obj).__name__,
                "kernel_twin_file": kernel_twin(name),
            }
            continue
        public_file = _rel(source_file)
        entry: dict[str, Any] = {
            "public_module": getattr(obj, "__module__", None),
            "public_file": public_file,
            "public_is_kernel": bool(public_file and "/kernel/" in public_file),
            "public_sha256": _digest(inspect.getsource(obj)),
        }
        twin = kernel_twin(name)
        entry["kernel_twin_file"] = twin
        if twin:
            src = (KERNEL_DIR.parent.parent / twin).read_text(encoding="utf-8")
            block = re.search(
                rf"^(?:class|def|async def) {re.escape(name)}\b"
                rf".*?(?=^class |^def |^async def |\Z)", src, re.M | re.S)
            entry["kernel_sha256"] = _digest(block.group(0)) if block else None
        pairs[name] = entry
    return {
        "schema_version": 1,
        "_comment": (
            "GOAL-3 / renquant-orchestrator#623 R1. Which copy of each DOCUMENTED public "
            "symbol executes, and the digest of both sides of every twin pair. Editing "
            "one side without the other fails the check --- that is the defect. "
            "Regenerate with tools/twin_pairs.py --emit and commit through review."
        ),
        "pairs": pairs,
    }


def verify(pins: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    want = pins.get("pairs") or {}
    if not want:
        return ["pin file lists no pairs — nothing is being checked"]
    live = survey()["pairs"]

    for name in sorted(set(want) - set(live)):
        problems.append(
            f"{name}: pinned but no longer a public export — re-emit so the pin "
            f"describes the actual surface")
    for name in sorted(set(live) - set(want)):
        problems.append(
            f"{name}: public export with no pin — run --emit and commit. An "
            f"unpinned export is indistinguishable from a checked one")

    for name in sorted(set(want) & set(live)):
        a, b = want[name], live[name]
        for field in ("public_module", "public_file", "kernel_twin_file"):
            if a.get(field) != b.get(field):
                problems.append(
                    f"{name}: {field} changed — reviewed against {a.get(field)!r}, now "
                    f"{b.get(field)!r}. WHICH COPY RUNS may have changed")
        if a.get("public_sha256") != b.get("public_sha256"):
            problems.append(
                f"{name}: the PUBLIC implementation changed"
                + ("" if a.get("kernel_sha256") == b.get("kernel_sha256")
                   else " (and so did the kernel twin)"))
        if a.get("kernel_sha256") != b.get("kernel_sha256") and \
                a.get("public_sha256") == b.get("public_sha256"):
            problems.append(
                f"{name}: the KERNEL twin changed while the public implementation did "
                f"NOT. This is exactly renquant-orchestrator#623 R1 — a fix applied to "
                f"the copy a reader finds first, leaving the documented symbol on the "
                f"old behaviour. Apply it to both or re-pin with a stated reason")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", action="store_true", help="print fresh pins (never writes)")
    ap.add_argument("--pins", type=pathlib.Path, default=PINS)
    args = ap.parse_args(argv)

    if args.emit:
        print(json.dumps(survey(), indent=2, sort_keys=True))
        return 0
    if not args.pins.exists():
        print(f"FATAL: pin file missing at {args.pins} — run --emit and commit it",
              file=sys.stderr)
        return 2
    try:
        pins = json.loads(args.pins.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: pin file unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    problems = verify(pins)
    if problems:
        print("\n".join(problems))
        print(f"\ntwin-pairs: {len(problems)} problem(s)")
        return 1
    n = len(pins["pairs"])
    twins = sum(1 for v in pins["pairs"].values() if v.get("kernel_twin_file"))
    print(f"twin-pairs OK — {n} public exports pinned, {twins} with a kernel twin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
