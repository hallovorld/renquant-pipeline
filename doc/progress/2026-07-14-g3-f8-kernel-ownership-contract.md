# G3 F-8: pinned kernel-ownership contract

**Date**: 2026-07-14
**PR**: pipeline fix/g3-f8-kernel-ownership-contract
**Companion**: orchestrator PR #514 (fix(bridge): fail-closed bootstrap alias
for pipeline kernel imports)

## Motivation

Orchestrator PR #514 fixes audit finding F-8: `bootstrap_multirepo()`'s alias
loop was silently falling back to umbrella copies of kernel modules whenever
the pinned pipeline's own copy failed to import. Round 1 of that fix added an
orchestrator-local `UMBRELLA_ONLY_STEMS` allowlist (fundamentals, macro,
drph, meta_label, ...) so only non-allowlisted stems hard-fail.

Codex (round 1 review) rejected that design: the allowlist is keyed off the
failing module's *name*, not off whether pipeline actually declares that
name as something it does not own. If a file happened to exist in the
pipeline kernel directory under one of those names and failed to import for
a real reason (syntax error, missing dependency), the orchestrator-side name
match would silently swallow the failure and let the stale umbrella copy
serve requests under that name — exactly the unpinned-fallback bug the PR
exists to close, just gated by a coincidental name instead of an unconditional
check. Codex asked for the ownership declaration to move to the pinned,
versioned pipeline side.

## Change

Added `renquant_pipeline.kernel.NON_OWNED_KERNEL_STEMS` — a frozenset
declared in `kernel/__init__.py` (the package that already documented, in
prose, the "lifted from umbrella" history of every module here). It lists
the only kernel-directory entries that are NOT part of the pinned,
guaranteed-importable contract: currently just `meta_label`, which is
physically present here (functional-lift copy) but never authoritative —
consumers force-alias `kernel.meta_label` to `renquant_backtesting.meta_label`
unconditionally regardless of whether this package's own copy imports
cleanly.

Every other entry in `kernel/` is now the enforced contract: a consumer
(orchestrator's `bootstrap_multirepo`) that finds an entry here and fails to
import it MUST treat that as fail-closed — the exemption is a structural
frozenset lookup owned by this package, never a name match maintained by a
downstream consumer.

## Why this closes the gap

- The declaration lives in the pinned, versioned pipeline package, not in
  orchestrator. Consumers read it via the already-imported `kernel` module
  object (see orchestrator PR #514 r3), so it travels with whatever pipeline
  commit is pinned.
- The exemption set is deliberately tiny (one entry, with a documented
  reason) rather than a broad name list — a future stem sharing a name with
  a classic "umbrella-only" module (e.g. a `fundamentals.py` landing in this
  directory by mistake) is NOT exempt unless pipeline itself explicitly adds
  it here with a reviewed reason. Import failure for it is a hard error.
- Missing the declaration entirely (an older pin without this attribute)
  causes the orchestrator to fail closed — it will not proceed without a
  verified ownership contract from the pinned pipeline.

## Verification (round 1-3)

- New `tests/test_kernel_ownership_contract.py`:
  - `NON_OWNED_KERNEL_STEMS` is a frozenset.
  - every declared stem still physically exists in `kernel/` (catches stale
    exemptions left behind after a module is deleted/renamed).
  - every kernel-directory entry NOT declared in `NON_OWNED_KERNEL_STEMS`
    imports cleanly — this is what makes the frozenset an enforced contract,
    not aspirational prose. Confirmed empirically: **every** current
    kernel-directory entry (including `meta_label` itself) imports cleanly
    today, so this declaration is a forward-looking guard, not covering an
    existing failure.
- Full suite: 1725 passed, 8 skipped (3 pre-existing failures — 2 in
  `test_replay_d6_conventions.py`, 1 in
  `test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`
  — reproduced identically on a clean `origin/main` checkout with none of
  this PR's changes, so unrelated to this change).

## Round 4: the companion `OWNED_KERNEL_STEMS` declaration

Codex's round-2 review on the orchestrator side (#514) raised a second,
independent concern in the same review comment as the `NON_OWNED_KERNEL_STEMS`
consistency issue above:

> Also do not use the arbitrary `_MIN_PIPELINE_KERNEL_MODULES = 10` as a
> path-identity control. It permits any wrong directory with ten importable
> files and will become stale when the package layout changes. Bind the
> discovered module inventory to the pinned package contract instead.

Orchestrator's `_MIN_PIPELINE_KERNEL_MODULES = 10` check existed to catch an
empty/wrong pipeline kernel directory silently producing zero (or
suspiciously few) aliases — e.g. a misconfigured checkout path pointing
somewhere that happens to contain a handful of unrelated importable files.
The number `10` was an orchestrator-local guess with no relationship to
what this package actually ships, and would both (a) miss a wrong directory
that happened to contain ten-plus unrelated files, and (b) go stale/require
manual bumping every time a real kernel module is added or removed here.

### Change

Added `renquant_pipeline.kernel.OWNED_KERNEL_STEMS` — the positive-side
companion to `NON_OWNED_KERNEL_STEMS`, built the same way (an explicit,
reviewed frozenset literal, not something computed at import time by
re-listing this same directory — a "contract" that just re-derives itself
from whatever is on disk would trivially "pass" against an empty or wrong
directory too, defeating the point). It lists all 49 stems currently in
`kernel/` that are NOT in `NON_OWNED_KERNEL_STEMS`.

Orchestrator's `bootstrap_multirepo` (PR #514 round 4) now reads this
declaration and verifies the stems it actually discovered in a pinned
checkout are a superset of everything declared here — this is the real,
pinned-contract-bound replacement for the old minimum-count heuristic.

### Tests (round 4)

Added to `tests/test_kernel_ownership_contract.py`:
- `test_owned_kernel_stems_is_frozen`
- `test_owned_kernel_stems_still_exist` (mirrors the existing
  `test_non_owned_kernel_stems_still_exist` for the positive side)
- `test_owned_and_non_owned_kernel_stems_are_disjoint`
- `test_declared_kernel_stems_match_directory_contents`: the real
  structural guarantee — `OWNED_KERNEL_STEMS | NON_OWNED_KERNEL_STEMS` must
  equal exactly what `kernel/` physically contains. This is what makes
  `OWNED_KERNEL_STEMS` an enforced, always-in-sync inventory rather than a
  hand-maintained list that silently drifts: adding, removing, or renaming
  a kernel module without updating one of these two declarations fails this
  test, in this repo's own CI, rather than surfacing later as an opaque
  orchestrator bootstrap error in a downstream repo.

### Cross-repo pairing verification

Confirmed against orchestrator PR #514's real worktree (both src roots on
`sys.path`, no mocks): orchestrator's real `bootstrap_multirepo` check
passes cleanly against this package's real `OWNED_KERNEL_STEMS` (49 stems)
and real `kernel/` directory contents (50 entries incl. `meta_label`), and
correctly fails closed — citing the exact stem name — when the declared
inventory is tampered with to include a stem absent from disk.

### Full suite verification (round 4)

Run with this package's own `.venv` (Python 3.11, matching its `Makefile`'s
standalone-interpreter resolution): 1729 passed, 8 skipped, 3 failed. The 3
failures are the same pre-existing ones documented in round 1-3 above
(`test_replay_d6_conventions.py` x2, `test_xgboost_scorer_contract.py` x1) —
reproduced identically with this round's diff stashed out on the same
worktree, confirming they predate and are unrelated to this change.

### PR note

Round 1-3 (`NON_OWNED_KERNEL_STEMS`) merged as PR #198 before this round's
commit landed on the branch (a push/merge race against the same branch).
Round 4 (`OWNED_KERNEL_STEMS`, this section) is a fresh PR on the same
branch: https://github.com/hallovorld/renquant-pipeline/pull/199.

Codex review (round 4) confirms: "the PR is rebased on current main, CI is
green, and OWNED_KERNEL_STEMS plus NON_OWNED_KERNEL_STEMS are enforced as a
disjoint, exact partition of the physical kernel inventory." Awaiting formal
APPROVE to unblock the #514 deployment chain.
