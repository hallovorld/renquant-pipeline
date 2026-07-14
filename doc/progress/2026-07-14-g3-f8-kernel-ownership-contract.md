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

## Verification

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
