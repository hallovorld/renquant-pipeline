# The twin registry catches an un-repinned edit. It could not catch the reverse.

**Date:** 2026-07-30 · GOAL-3 (architecture compliance) · `renquant-pipeline`
**Follows:** #231 (the registry), orchestrator#623 R1 (the defect it registers)

**Bottom line:** `verify()` compares each twin's committed digest to the live file,
so an edit that was **never re-pinned** fails the check. The opposite order slips
through: **edit one twin, re-emit, commit.** File and pin move together, `verify()`
is clean, and the divergence quietly becomes the reviewed baseline. The only place
it shows is the pin diff, where a human has to notice that one digest moved and its
partner did not — and #623 records **four** occasions where the wrong object was
filled in because nobody did.

## 1. What was and was not already covered

Measured on `origin/main` before touching anything
`[VERIFIED — pytest tests/test_twin_pairs.py, 2026-07-30]`:

- The registry is **not** inert scaffolding. `verify()` reports R1 (kernel changed,
  public did not) with a real message, and its 17 tests pass.
- CI runs `make test`, which is `pytest -q`, so the check **is** live on every push
  `[VERIFIED — .github/workflows/ci.yml, Makefile:17]`.
- `--emit` **prints and never writes**, so the pin file only changes through a
  reviewed PR. That is a deliberate, good design.

None of that closes the re-pin path, because after a re-pin there is nothing left
for `verify()` to disagree with.

## 2. What this adds

`one_sided_repins(old, new)` — a pure function over two pin files, normally the PR's
base and head. It reports every pair where **exactly one** digest changed.

It is **not a prohibition** — and after review, that is now true in the code as well
as in this sentence. **The first version said "state a reason" and gave CI nowhere to
state one**, so in practice it was an unconditional prohibition that would either
block legitimate kernel-only work or push an author to touch an unrelated twin purely
to appease CI `[codex BLOCKER on #232, accepted]`.

`twin_repin_exceptions.json` closes that. An entry suppresses the finding for
**exactly one pair moving from one exact digest tuple to one exact digest tuple**:

```json
{"pair": "...", "old_public_sha256": "...", "old_kernel_sha256": "...",
 "new_public_sha256": "...", "new_kernel_sha256": "...", "reason": "..."}
```

Three properties make it a justification rather than a licence:

- **It cannot be pre-written.** The new digests do not exist until the change does.
- **It cannot be reused.** Move either side again and the tuple no longer matches.
- **A leftover entry is REPORTED as stale**, because an allowlist whose entries
  outlive their change silently pre-authorises the next one — the failure mode every
  allowlist has.

`reason` must be non-empty: a justification with no justification is a rubber stamp.
An **unreadable** exception file is FATAL (exit 2), never "no exceptions".

Wired as a `pull_request`-only CI step. On a plain push there is no base ref, and
inventing one would make the check pass for the wrong reason. `fetch-depth: 0` is set
on the checkout in the same commit, because a shallow clone would make the step
**silently skip** — the failure mode this whole registry exists to prevent.

## 3. Deliberate non-coverage, each with a test

- **Pairs with no kernel twin are skipped.** "One-sided" is undefined without a
  second side, and 3 of the 9 pinned exports have none. Reporting them would train
  the reader to ignore the output.
- **Added or removed pairs are not reported here.** Those are `verify()`'s job
  (unpinned export / pinned-but-gone). Double-reporting would blur which check owns
  which failure.
- **A missing or unreadable baseline is FATAL (exit 2), never clean.** A baseline
  that cannot be read must not read as agreement — the same fail-open shape codex
  rejected on the umbrella scan's sibling side earlier today.

## 4. Suite

`tests/test_twin_pairs_one_sided_repin.py` — 11 new tests including **two
anti-vacuity controls**: both digests moving must **not** be reported (a check that
flags every pin update gets ignored within a week), and the CLI comparing the pins
to themselves must exit 0 (otherwise the exit-1 test proves nothing).

Together with the existing suite: **28 passed** `[VERIFIED — pytest, this session]`.

## Review round 1 — the exception policy broke every later PR

Codex: an exception is used on the PR that adds it, but after merge the next unrelated
PR has no matching pin movement, so `one_sided_repins()` reported the same committed
entry as STALE and CI failed **permanently**. A legitimate, justified exception was a
time bomb.

**The check's subject was wrong.** It asked *"did this diff use the entry"* when the
question is *"does the entry still describe the pins"*. An exception is an audit record
of a movement that happened — not a token consumed by one diff — so it stays true for
as long as the pair sits at its `new_*` tuple.

Rewritten against `new` rather than against the diff. An entry is now reported when:

* its `reason` is empty (unchanged — a rubber stamp);
* it is missing a required key, so it cannot be bound to a movement at all;
* it names a pair that no longer exists in the pins;
* **SUPERSEDED** — the pins have moved past its `new_*` tuple, so it describes a state
  that no longer exists and would pre-authorise a movement nobody justified.

That last case preserves the property the original comment wanted (*"goes stale the
moment either side moves again"*) — which was always checkable against the pins, and
never needed the diff.

**Regression, exactly as asked:** `test_a_CURRENT_exception_survives_an_unrelated_later_PR`
puts the pins at the settled tuple with nothing moving — every subsequent PR's baseline —
and requires it clean. `test_the_landing_PR_and_the_NEXT_one_both_pass_with_the_same_entry`
asserts both halves together, since each alone can pass while the pair is broken.

`[VERIFIED — this session]` 21 tests pass. Load-bearing confirmed by restoring the old
"not used by this diff" rule: both new regressions **fail**, and all 21 pass again on
restore.

The committed `_comment` was updated too — it described the old semantics, and a policy
file that misstates its own rule is how the next author re-derives the bug.

---

## ROUND 2 2026-08-01 — the record could be deleted after it landed, and a malformed file crashed

Two findings `[codex on #232]`, both real.

### 1. Delete-after-landing

> *"CI lets a later PR delete that record silently. If the base has an exception whose
> new tuple still equals the proposed pins, and the head removes it while leaving the
> pins unchanged, `one_sided_repins` receives an empty list and passes."*

**That is this check's own defect one level up: the guard passed because its subject had
been removed.** An exception is a committed audit record of a divergence that is *still
in force* while the pins sit at its `new` tuple, so deleting it discards the
justification without any re-pin.

`removed_live_exceptions(base_exc, head_exc, base_pins, head_pins)` fails on exactly that
case, and CI now hands it the base ref's exception file. Three deletions stay legitimate,
each with a test:

| deletion | verdict | why |
|---|---|---|
| record still applies, pins unchanged | **FAIL** | the justification is still load-bearing |
| the pair moved again | allow | the record has aged out; keeping it is the `SUPERSEDED` case this file already reports |
| this PR re-pins the same pair | allow | `one_sided_repins` already demands a fresh justification — a second finding for one fact would push authors to keep stale records to silence it |
| the export vanished | allow | nothing is pinned at that tuple any more |

An absent exception file **on the base** is legitimate (none existed) and becomes an empty
list; an absent file passed explicitly via `--base-exceptions` is **fatal**, because an
absent baseline cannot be shown to have kept its records.

### 2. A malformed exception file crashed instead of failing closed

> *"a syntactically valid file such as `{"exceptions":[7]}` reaches `e.get` and crashes
> rather than producing the explicit fail-closed diagnostic this CI guard promises."*

`load_exceptions` now validates the shape and raises `ExceptionFileError` naming the
offending index and type. **A crash and a diagnostic are both non-zero; only one tells the
author what to fix, and only one is distinguishable from the tool itself being broken** —
which matters most for a file whose only job is to *suppress* findings.

Six malformed shapes are parametrised (`{"exceptions": 7}`, `[7]`, `["a"]`, `[{}, 3]`,
a missing key, a bare `7`), paired with a well-formed case and the repo's own committed
file so the validator cannot pass by rejecting everything.

Tests 20 → 34. Verified end-to-end through `main()`, not only at the function boundary:
the delete-after-landing PR shape returns **rc=1** with the diagnostic above.

### Two defects of my own, caught while doing this

- my `--base-exceptions` argparse insertion matched `parser.add_argument`, but the
  variable is `ap` — **it silently inserted nothing** while I printed "wired". Found by
  running `--help`, not by grep;
- the appended tests redefined a module-level `_pins` helper that already existed, which
  would have **changed the behaviour of every test above them**. Renamed.
