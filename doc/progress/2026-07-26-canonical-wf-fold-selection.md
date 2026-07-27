# Canonical walk-forward fold-selection contract consumption (common#33)

Date: 2026-07-26
Trigger: Codex review on `hallovorld/renquant-common#33` — "the live
`WalkForwardModelLoader` still has an inline implementation and does not
import it... include the loader refactor and a contract-parity test that
calls the live loader and shared selector on the same manifest/boundaries."
This PR is that loader-consumption condition.

## What changed

- `src/renquant_pipeline/kernel/walk_forward/loader.py` —
  `WalkForwardModelLoader` no longer carries its own inline copy of the PIT
  fold eligibility/selection date arithmetic. It imports
  `renquant_common.walk_forward_fold_selection` (common#33) at module top
  and delegates:
  - `_feature_cutoff_date` → `feature_cutoff_date` (adapter staticmethod
    kept, same signature — `renquant_backtesting`'s subclass and existing
    tests keep working; it only overrides `_resolve_uri`).
  - `_safe_last_label_date` → `safe_last_label_date`.
  - `entry_as_of`'s eligibility filter + `eligible[-1]` selection →
    `select_latest_eligible_fold`. The "no eligible fold" ValueError
    (P1 contract: sims abort loudly) stays in the loader; the shared
    selector's `None` triggers it, message unchanged.
- Byte-identical tie handling: the shared selector uses `max` (returns the
  FIRST maximal element) while the loader's historical `eligible[-1]` over
  its stably-ascending-sorted entries picks the LAST row among a duplicated
  `cutoff_date`. The loader feeds the reversed (descending) entries list so
  first-max == the historical last-among-ties — behavior preserved even in
  the degenerate duplicate-cutoff case. (The common#33 module docstring's
  "last-in-max wins" wording is inaccurate for Python `max`; flagged on the
  PR thread. Well-formed manifests never duplicate `cutoff_date`, so this
  only pins the degenerate case.)
- `pyproject.toml` — `renquant-common>=0.15.0,<1.0` (structural: the module
  does not exist below 0.15.0; loader fails to import).

## Sequencing (tight)

This PR must merge IMMEDIATELY AFTER common#33, same batch. Pipeline CI
checks out `renquant-common`'s default branch, so until common#33 merges,
`renquant_common.walk_forward_fold_selection` does not exist on CI and this
branch's CI is red (ImportError at loader import). Local verification below
ran with the common#33 branch on `PYTHONPATH` to prove the pairing green.

## Parity-test coverage

`tests/test_wf_fold_selection_parity.py` (29 tests) drives BOTH the live
loader path (`entry_as_of` over a real JSON manifest on disk) and the
shared selector directly (structural fold records from the same fixture
rows) and asserts identical selections:

- delegation-is-an-import identity check (loader names ARE the common
  functions, not a fourth copy);
- helper parity grid (Friday/midweek cutoffs × lookahead 0/1/60 ×
  effective absent/empty-string/present) + business-day-vs-calendar-day
  divergence pin;
- empty manifest; window before all coverage; latest-eligible-wins;
- out-of-order manifest rows (loader sorts at parse, selector is
  order-free — same pick);
- strict-`<` boundary-date ties (prediction exactly ON the safe-last-label
  date, lookahead 1 and 0);
- embargo overlaps: pre-embargoed `effective_train_cutoff_date` admits a
  fold whose plain cutoff is still in the future (the renquant-model#64
  bug class) incl. the tie exactly on the embargo edge; newest fold still
  inside its 60-BDay label window skipped for the older safe fold;
- degenerate duplicate-`cutoff_date` tie: loader's historical
  last-manifest-row pick pinned as a regression; shared-selector list-order
  sensitivity pinned explicitly.

## Verification

- `PYTHONPATH="<common-33-worktree>/src:src" python -m pytest tests -q`
  (umbrella `.venv` python): **2007 passed, 8 skipped, 0 failed** — full
  suite, no pre-existing failures in this environment. `[VERIFIED]`
- Parity file alone: 29/29. WF-loader neighbors
  (`test_walkforward_loader_uri_resolution.py`, `test_wf_replay_loader.py`)
  green in the same run.

## Revert

`git revert` of this single commit (loader + pyproject + tests + this doc);
no data/artifact/state migration involved.
