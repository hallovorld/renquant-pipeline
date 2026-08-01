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


EXCEPTIONS = pathlib.Path(__file__).resolve().parent.parent / "twin_repin_exceptions.json"


def _pair_digests(entry: dict) -> tuple[str | None, str | None]:
    return entry.get("public_sha256"), entry.get("kernel_sha256")


class ExceptionFileError(ValueError):
    """The exception file is syntactically valid JSON but not the shape we promise.

    Raised rather than tolerated: this file's only job is to SUPPRESS findings, so a
    shape we cannot read must fail closed and loudly. Reviewed `[codex on #232]`:
    *"a syntactically valid file such as `{"exceptions":[7]}` reaches `e.get` and
    crashes rather than producing the explicit fail-closed diagnostic this CI guard
    promises."* A crash and a diagnostic are both non-zero, but only one tells the
    author what to fix.
    """


def load_exceptions(path: pathlib.Path | None = None) -> list[dict]:
    """Committed justifications for one-sided re-pins. Missing file == no exceptions.

    STRUCTURE IS CHECKED, not assumed. `{"exceptions": 7}`, `{"exceptions": [7]}` and
    a bare `7` are all valid JSON and none is an exception list; each used to reach
    `e.get(...)` inside the comparison and die with an AttributeError three frames
    away from the file that caused it.
    """
    target = path or EXCEPTIONS
    if not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        if "exceptions" not in data:
            raise ExceptionFileError(
                f"{target.name}: object with no 'exceptions' key — "
                f"found {sorted(data)!r}")
        items = data["exceptions"]
    else:
        items = data
    if items is None:
        return []
    if not isinstance(items, list):
        raise ExceptionFileError(
            f"{target.name}: 'exceptions' must be a list, got "
            f"{type(items).__name__}")
    for i, e in enumerate(items):
        if not isinstance(e, dict):
            raise ExceptionFileError(
                f"{target.name}: exceptions[{i}] must be an object, got "
                f"{type(e).__name__} ({e!r})")
    return list(items)


def removed_live_exceptions(base_exc: list[dict], head_exc: list[dict],
                            old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Exceptions that were APPLICABLE on the base and are gone from the head.

    Reviewed `[codex on #232]`: *"CI lets a later PR delete that record silently. If
    the base has an exception whose new tuple still equals the proposed pins, and the
    head removes it while leaving the pins unchanged, `one_sided_repins` receives an
    empty list and passes. That loses the justification/provenance without any
    re-pin."*

    Exactly right, and it is this check's own shape one level up: the guard passed
    because its subject had been removed. An exception is a committed audit record of
    a divergence that is STILL IN FORCE while the pins sit at its `new` tuple — so
    deleting it is only legitimate if the pair has since moved again, which is the
    case this function excludes.
    """
    problems: list[str] = []
    head_keys = {_exception_key(e) for e in head_exc}
    a, b = old.get("pairs") or {}, new.get("pairs") or {}
    for e in base_exc:
        key = _exception_key(e)
        if key in head_keys:
            continue
        name = e.get("pair")
        after_pub = e.get("new_public_sha256")
        after_ker = e.get("new_kernel_sha256")
        cur = b.get(name) or {}
        still_applies = (
            name in b
            and cur.get("public_sha256") == after_pub
            and cur.get("kernel_sha256") == after_ker
        )
        if not still_applies:
            continue          # the pair moved again -- the record has aged out
        if (a.get(name) or {}) != cur:
            continue          # this PR re-pins the pair, so a fresh justification is due
        problems.append(
            f"{name}: an exception that STILL APPLIES was deleted while the pins did "
            f"not move. The pair is pinned at exactly the tuple this record justifies "
            f"({str(after_pub)[:12]}/{str(after_ker)[:12]}), so removing it discards the "
            f"justification for a divergence that is still in force. Re-pin the pair "
            f"with a fresh justification, or keep the record."
        )
    return problems


def _exception_key(e: dict) -> tuple:
    """Identity of an exception: the pair plus both digest tuples it blesses.

    FLATTENED keys, which is the schema this file actually uses -- the committed
    `_comment` names them and `one_sided_repins` reads them. My first version of this
    helper read a NESTED `old`/`new` shape that exists nowhere `[codex on #232]`, so
    every lookup returned `None` and the deletion guard could never fire. The tests
    passed because I had written their fixtures in the same invented schema: a guard and
    its test agreeing with each other about a shape the data does not have.
    """
    return (e.get("pair"),
            e.get("old_public_sha256"), e.get("old_kernel_sha256"),
            e.get("new_public_sha256"), e.get("new_kernel_sha256"))


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

    # STALENESS IS ABOUT THE PINS, NOT ABOUT THIS DIFF.
    #
    # The first version reported every exception not used by the CURRENT diff. That
    # makes a legitimate exception a time bomb: it is used on the PR that adds it,
    # and then the very next unrelated PR has no matching pin movement, so the same
    # committed entry is reported and CI fails permanently. The check's subject was
    # "did this diff use it" when the question is "does it still describe the pins".
    #
    # An exception is a record that a specific pair moved from one exact digest tuple
    # to another, with a reason. It remains TRUE as long as the pair still sits at the
    # `new_*` tuple. It is superseded the moment either side moves again -- which is
    # the property the original comment wanted, and it is checkable against `new`
    # rather than against the diff.
    for e in exc:
        name = e.get("pair")
        why = str(e.get("reason") or "").strip()
        if not why:
            problems.append(
                f"{name or '?'}: exception states no reason. A justification with no "
                f"justification is a rubber stamp")
            continue
        missing = [k for k in ("pair", "old_public_sha256", "old_kernel_sha256",
                               "new_public_sha256", "new_kernel_sha256")
                   if not e.get(k)]
        if missing:
            problems.append(
                f"{name or '?'}: exception is missing required key(s) {missing} — it "
                f"cannot be bound to a digest movement and cannot be checked")
            continue
        if name not in b:
            problems.append(
                f"{name}: exception names a pair that no longer exists in the pins — "
                f"remove it, the movement it records can no longer be verified")
            continue
        cur = _pair_digests(b[name])
        if (e.get("new_public_sha256"), e.get("new_kernel_sha256")) != cur:
            problems.append(
                f"{name}: SUPERSEDED exception in twin_repin_exceptions.json — it "
                f"records a move to {e.get('new_public_sha256', '?')[:12]}…/"
                f"{e.get('new_kernel_sha256', '?')[:12]}… but the pins now read "
                f"{(cur[0] or '?')[:12]}…/{(cur[1] or '?')[:12]}…. It no longer "
                f"describes the current state, and leaving it would pre-authorise a "
                f"movement nobody justified. Remove it or re-declare the new one")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", action="store_true", help="print fresh pins (never writes)")
    ap.add_argument("--base-exceptions", metavar="EXCEPTIONS", default=None,
                    help="the BASE ref's twin_repin_exceptions.json; without it a "
                         "still-applicable record can be deleted silently")
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

        # The BASE's exception file, when CI supplies it. Without this, deleting a
        # still-applicable record passes silently: one_sided_repins sees an empty list
        # and finds nothing to complain about.
        base_exc: list[dict] = []
        if args.base_exceptions:
            base_exc_path = pathlib.Path(args.base_exceptions)
            if not base_exc_path.exists():
                print(f"FATAL: --base-exceptions given but missing at {base_exc_path} "
                      f"— an absent baseline cannot be shown to have kept its records",
                      file=sys.stderr)
                return 2
            try:
                base_exc = load_exceptions(base_exc_path)
            except Exception as e:  # noqa: BLE001
                print(f"FATAL: base exception file unreadable: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
                return 2

        blessed = one_sided_repins(base, pins, exceptions=exc)
        deleted = removed_live_exceptions(base_exc, exc, base, pins)
        if blessed or deleted:
            print("\n".join(blessed + deleted))
            print(f"\ntwin-pairs: {len(blessed)} one-sided re-pin(s), "
                  f"{len(deleted)} deleted live exception(s)")
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
    print(f"twin-pairs OK — {n} public exports pinned, {twins} with a kernel twin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
