# fix(V-003): import decision_ledger from common, not orchestrator

**Date**: 2026-07-13
**PR**: pipeline fix/v003-import-from-common

## Change

Update `task_decision_ledger.py` to import `connect`/`write_verdicts` from
`renquant_common.decision_ledger` instead of `renquant_orchestrator.decision_ledger`.

This eliminates the reverse dependency (pipeline → orchestrator) that violates
the subrepo operating model's dependency direction.

## Dependency

Requires common PR #30 (move persistence functions to common) to merge first.

## Follow-up: stale test mocks (found by independent review)

`tests/test_task_decision_ledger.py`'s autouse fixture still registered a fake
`renquant_orchestrator.decision_ledger` in `sys.modules` and every test still
patched `renquant_orchestrator.decision_ledger.connect` /
`.write_verdicts` — targets the production code no longer imports at all.
Once `renquant_common.decision_ledger` becomes importable (post common#30),
those patches would intercept nothing, and the real unmocked
`connect()`/`write_verdicts()` would execute against the default sqlite path
(`~/renquant-data/decision_ledger.db`).

Fixed:

- The autouse fixture now fakes `renquant_common.decision_ledger` (shape
  matched against common#30's actual `src/renquant_common/decision_ledger.py`:
  `connect`, `write_verdicts`, `DDL`, `DEFAULT_DB`, `_VALID_VERDICTS`), and all
  `patch(...)` targets were updated to match.
- Critically, unlike the old orchestrator fake, `renquant_common` is a REAL
  dependency `renquant_pipeline.__init__` already imports (`Job`, `Pipeline`,
  `Task`). A naive port of the old pattern — replacing
  `sys.modules["renquant_common"]` wholesale — shadowed those real exports
  and broke every test with `ImportError: cannot import name 'Job'`. The
  fixture now reuses the real `renquant_common` module object when
  importable and only swaps in the fake `decision_ledger` submodule/attribute.
- Stale docstrings in `src/renquant_pipeline/decision_ledger.py` (module
  docstring + `format_gate_verdicts`) and
  `task_decision_ledger.py`'s class docstring still referenced
  `renquant_orchestrator.decision_ledger`; updated to `renquant_common.decision_ledger`.
  (`ledger_attribution.py` references were left untouched — that module has
  not moved.)

### Verification (against the real common#30 shape, not a guess)

common#30 hasn't merged to `renquant-common@main` yet, so this repo's own
`renquant_common` sibling checkout doesn't have `decision_ledger.py`. Verified
by adding a git worktree of the actual `renquant-common` `fix/v003-decision-ledger-to-common`
branch as the `../renquant-common` sibling and running the suite against it:

- `python3 -m pytest tests/test_task_decision_ledger.py -v` → 8/8 pass.
- Full suite: 51 failed / 1667 passed before vs. after — the 51 are
  pre-existing/environmental (missing xgboost/scipy in this bare interpreter,
  unrelated to V-003); confirmed by `git stash` diffing failure counts
  (55 failed pre-fix — the same 51 plus these 4 — vs. 51 post-fix).
- Ran the **unmodified pre-fix** test file against the same real common#30
  module: 4/8 tests fail (`test_enabled_calls_formatters`,
  `test_failopen_on_orchestrator_import_error`,
  `test_failopen_on_write_exception`, `test_verdicts_shape_passed_to_write`),
  and — concretely, not hypothetically — this run left real
  `2026-07-01-daily-full` rows in `~/renquant-data/decision_ledger.db` (the
  actual live-run decision ledger DB on this machine), because the old
  mocks never intercepted the real `connect()`/`write_verdicts()` call. This
  reproduces exactly the silent-unmocked-write failure mode described above.
  The spurious rows were identified (uniquely fixture-shaped `run_id`, not
  matching any live-run naming pattern) and deleted to restore the DB.

## PR checklist

"Pipeline tests pass with common providing `decision_ledger`" can be checked
off — verified above against common#30's actual branch content.
