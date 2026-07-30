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
