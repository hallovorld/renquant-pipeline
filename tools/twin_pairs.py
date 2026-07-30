#!/usr/bin/env python3
"""Pin every public Task export and its same-named kernel twin. (GOAL-3, orch#623 R1)

The orchestrator's twin registry (renquant-orchestrator#623) names the defect: *"the
failure is not that duplicates exist — some duplication is deliberate. The failure is
that nothing in the repo tells you which copy executes."* Its row R1 is this package:
``renquant_pipeline.VetoWeakBuysTask``, the **documented** symbol, resolves to
``panel_scoring.py`` and not to the kernel, so a kernel-only fix misses it. That cost a
real defect (pipeline#222).

Measured here, R1 is not a one-off:

* **9 of 9** public ``*Task`` exports resolve to a **non-kernel** module;
* **6 of those 9** have a **same-named class under ``kernel/``**.

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


def public_task_names(module) -> list[str]:
    """Public ``*Task`` exports, read off ``__all__``.

    ``__all__`` and not ``dir()``: the documented surface is the one a caller is told to
    import, and it is the surface #623 R1 is about.
    """
    return sorted(n for n in getattr(module, "__all__", []) if n.endswith("Task"))


def kernel_twin(name: str) -> str | None:
    """Path of a same-named class under ``kernel/``, or None."""
    pattern = re.compile(rf"^class {re.escape(name)}\b", re.M)
    for path in sorted(KERNEL_DIR.rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            return _rel(str(path))
    return None


def survey() -> dict[str, Any]:
    import renquant_pipeline as rp

    pairs: dict[str, Any] = {}
    for name in public_task_names(rp):
        obj = getattr(rp, name)
        public_file = _rel(inspect.getsourcefile(obj))
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
                rf"^class {re.escape(name)}\b.*?(?=^class |\Z)", src, re.M | re.S)
            entry["kernel_sha256"] = _digest(block.group(0)) if block else None
        pairs[name] = entry
    return {
        "schema_version": 1,
        "_comment": (
            "GOAL-3 / renquant-orchestrator#623 R1. Which copy of each DOCUMENTED Task "
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
            f"{name}: pinned but no longer a public Task export — re-emit so the pin "
            f"describes the actual surface")
    for name in sorted(set(live) - set(want)):
        problems.append(
            f"{name}: public Task export with no pin — run --emit and commit. An "
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


EXCEPTIONS = pathlib.Path(__file__).resolve().parent.parent / "twin_repin_exceptions.json"


def _pair_digests(entry: dict) -> tuple[str | None, str | None]:
    return entry.get("public_sha256"), entry.get("kernel_sha256")


def load_exceptions(path: pathlib.Path | None = None) -> list[dict]:
    """Committed justifications for one-sided re-pins. Missing file == no exceptions."""
    target = path or EXCEPTIONS
    if not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    items = data.get("exceptions") if isinstance(data, dict) else data
    return list(items or [])


def one_sided_repins(old: dict[str, Any], new: dict[str, Any],
                     exceptions: list[dict] | None = None) -> list[str]:
    """Pairs whose pin update BLESSES a change to only one side, minus justified ones.

    `verify()` catches an edit that was never re-pinned. It cannot catch the other
    order: edit one twin, re-emit, commit. Both the file and its pin move together,
    so `verify()` is clean and the divergence is now the reviewed baseline. The only
    place it shows is the pin DIFF, where a reviewer has to notice that
    `public_sha256` did not move while `kernel_sha256` did.

    A one-sided change CAN be legitimate -- a comment, a kernel-only private helper.
    The first version of this function said so and then gave CI no way to say it,
    which made the check an unconditional prohibition that would either block real
    kernel-only work or push authors to touch an unrelated twin to appease CI (codex
    BLOCKER on #232). An exception therefore suppresses a finding, but ONLY when it
    names the pair and BOTH exact digest tuples -- so it cannot be written in advance,
    cannot be reused for the next change, and expires the moment either side moves
    again.
    """
    problems: list[str] = []
    a, b = old.get("pairs") or {}, new.get("pairs") or {}
    exc = exceptions if exceptions is not None else load_exceptions()

    def _justified(name: str) -> dict | None:
        for e in exc:
            if e.get("pair") != name:
                continue
            if (e.get("old_public_sha256"), e.get("old_kernel_sha256")) == _pair_digests(a[name]) \
               and (e.get("new_public_sha256"), e.get("new_kernel_sha256")) == _pair_digests(b[name]):
                return e
        return None

    used: list[int] = []
    for name in sorted(set(a) & set(b)):
        pub_moved = a[name].get("public_sha256") != b[name].get("public_sha256")
        ker_moved = a[name].get("kernel_sha256") != b[name].get("kernel_sha256")
        if not b[name].get("kernel_twin_file"):
            continue          # no twin, so "one-sided" is not defined for it
        if pub_moved == ker_moved:
            continue          # both moved, or neither
        hit = _justified(name)
        if hit is not None:
            if not str(hit.get("reason") or "").strip():
                problems.append(
                    f"{name}: an exception matches the digests but states no reason. "
                    f"A justification with no justification is a rubber stamp")
                continue
            used.append(exc.index(hit))
            continue
        side = "PUBLIC-only" if pub_moved else "KERNEL-only"
        tail = ("This is the #623 R1 shape arriving through the pin file instead of "
                "past it. " if ker_moved else "")
        problems.append(
            f"{name}: this pin update blesses a {side} change — the other side's "
            f"digest is unchanged. {tail}Apply the change to both, or commit an entry "
            f"in twin_repin_exceptions.json naming this pair and BOTH digest tuples "
            f"with a reason")

    for i, e in enumerate(exc):
        if i in used:
            continue
        problems.append(
            f"{e.get('pair', '?')}: STALE exception in twin_repin_exceptions.json — it "
            f"matches no one-sided re-pin in this diff. Exceptions are bound to one "
            f"exact digest tuple; a leftover one silently pre-authorises the NEXT "
            f"change. Remove it")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", action="store_true", help="print fresh pins (never writes)")
    ap.add_argument("--diff-against", metavar="PINS",
                    help="compare the committed pins against an earlier pin file "
                         "(normally the PR base) and report one-sided re-pins")
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

    if args.diff_against:
        base_path = pathlib.Path(args.diff_against)
        if not base_path.exists():
            print(f"FATAL: base pin file missing at {base_path} — a missing baseline "
                  f"cannot be shown to agree with anything", file=sys.stderr)
            return 2
        try:
            base = json.loads(base_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: base pin file unreadable: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2
        try:
            exc = load_exceptions()
        except Exception as e:  # noqa: BLE001
            print(f"FATAL: twin_repin_exceptions.json unreadable: "
                  f"{type(e).__name__}: {e} — an unreadable exception file must not "
                  f"read as 'no exceptions'", file=sys.stderr)
            return 2
        blessed = one_sided_repins(base, pins, exceptions=exc)
        if blessed:
            print("\n".join(blessed))
            print(f"\ntwin-pairs: {len(blessed)} one-sided re-pin(s)")
            return 1
        print("twin-pairs: no one-sided re-pins against the given baseline")
        return 0

    problems = verify(pins)
    if problems:
        print("\n".join(problems))
        print(f"\ntwin-pairs: {len(problems)} problem(s)")
        return 1
    n = len(pins["pairs"])
    twins = sum(1 for v in pins["pairs"].values() if v.get("kernel_twin_file"))
    print(f"twin-pairs OK — {n} public Task exports pinned, {twins} with a kernel twin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
