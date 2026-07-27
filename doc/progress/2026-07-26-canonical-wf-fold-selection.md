# Canonical walk-forward fold-selection contract consumption (common#33)   (PR #214)

STATUS:    delivered
WHAT:      `WalkForwardModelLoader` drops its inline PIT fold-eligibility/selection
           date arithmetic and delegates to `renquant_common.walk_forward_fold_selection`
           (common#33): `_feature_cutoff_date` → `feature_cutoff_date`,
           `_safe_last_label_date` → `safe_last_label_date`, `entry_as_of`'s eligibility
           filter + `eligible[-1]` → `select_latest_eligible_fold`. Adds
           `tests/test_wf_fold_selection_parity.py` (29 tests) pinning byte-identical
           selection between the live loader and the shared selector on the same
           manifest/boundary fixtures, including the degenerate duplicate-`cutoff_date`
           tie (loader feeds the selector reversed entries so `max`'s first-max
           reproduces the historical `eligible[-1]` last-among-ties pick).
WHY/DIR:   Closes the loader-consumption condition Codex raised on
           `renquant-common#33`'s review — a shared selector no consumer imports is
           dead infrastructure (§7.7 anti-decoration). Makes
           `renquant_common.walk_forward_fold_selection` the single source of truth
           (§7.5) for PIT fold eligibility instead of a duplicated inline copy in the
           loader.
EVIDENCE:
  artifact:      src/renquant_pipeline/kernel/walk_forward/loader.py +
                 tests/test_wf_fold_selection_parity.py
  prod or exp:   prod (WalkForwardModelLoader is on the live inference path)
  existing data: full suite run with the common#33 branch on PYTHONPATH (the module
                 does not exist on this repo's main yet, so CI is red until common#33
                 merges first) — 2007 passed, 8 skipped, 0 failed; the parity file
                 alone 29/29; no pre-existing failures in this environment
  best-known?:   n/a — behavior-preserving refactor + parity test, not a new
                 model/metric variant; no IC/Sharpe/APY claim is made
  scope:         "this is a structural delegation (prod path) + parity test proving
                 the live loader and the shared selector agree on every fixture
                 boundary tried; not a performance claim"
NEXT:      Merge `renquant-common#33` first (this branch's CI is red until then —
           ImportError on `renquant_common.walk_forward_fold_selection`), then merge
           this PR immediately after, same batch.

## Sequencing (tight)

This PR must merge IMMEDIATELY AFTER common#33, same batch. Pipeline CI checks out
`renquant-common`'s default branch, so until common#33 merges,
`renquant_common.walk_forward_fold_selection` does not exist on CI and this branch's
CI is red (ImportError at loader import). Local verification below ran with the
common#33 branch on `PYTHONPATH` to prove the pairing green.

## Parity-test coverage

`tests/test_wf_fold_selection_parity.py` (29 tests) drives BOTH the live loader path
(`entry_as_of` over a real JSON manifest on disk) and the shared selector directly
(structural fold records from the same fixture rows) and asserts identical
selections:

- delegation-is-an-import identity check (loader names ARE the common functions,
  not a fourth copy);
- helper parity grid (Friday/midweek cutoffs × lookahead 0/1/60 × effective
  absent/empty-string/present) + business-day-vs-calendar-day divergence pin;
- empty manifest; window before all coverage; latest-eligible-wins;
- out-of-order manifest rows (loader sorts at parse, selector is order-free — same
  pick);
- strict-`<` boundary-date ties (prediction exactly ON the safe-last-label date,
  lookahead 1 and 0);
- embargo overlaps: pre-embargoed `effective_train_cutoff_date` admits a fold whose
  plain cutoff is still in the future (the renquant-model#64 bug class) incl. the
  tie exactly on the embargo edge; newest fold still inside its 60-BDay label
  window skipped for the older safe fold;
- degenerate duplicate-`cutoff_date` tie: loader's historical last-manifest-row
  pick pinned as a regression; shared-selector list-order sensitivity pinned
  explicitly.

## Verification

- `PYTHONPATH="<common-33-worktree>/src:src" python -m pytest tests -q` (umbrella
  `.venv` python): **2007 passed, 8 skipped, 0 failed** — full suite, no
  pre-existing failures in this environment. `[VERIFIED]`
- Parity file alone: 29/29. WF-loader neighbors
  (`test_walkforward_loader_uri_resolution.py`, `test_wf_replay_loader.py`) green
  in the same run.

## Revert

`git revert` of this single commit (loader + pyproject + tests + this doc); no
data/artifact/state migration involved.
