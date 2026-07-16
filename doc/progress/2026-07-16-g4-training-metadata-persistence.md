# G4: Persist training_cutoff + model_content_sha256 in pipeline_runs

Date: 2026-07-16

## Problem

The G4 ensemble experiment (Phase A) requires admissible score evidence
from `runs.alpaca.db`. The canonical admissibility validator
(`admissibility_ledger.build_ledger`) rejects ALL backfilled scores
because `pipeline_runs` never persists `training_cutoff` or
`model_content_sha256` — every record carries `"MISSING"` for both
fields, triggering fail-closed rejection.

This is the structural DATA-BOUND blocker for G4 L1.

## Solution

Add `training_cutoff TEXT` and `model_content_sha256 TEXT` columns to the
`pipeline_runs` table in persistence.py:

1. **Schema**: two new nullable columns after `commit_sha`
2. **Migration**: added to `_COLUMN_MIGRATIONS["pipeline_runs"]` so
   existing production DBs gain the columns on next `ensure_schema()`
3. **API**: `record_pipeline_run()` gains two optional keyword parameters
   (default `None`) — fully backwards-compatible with all existing callers
4. **Write path**: values are persisted in the INSERT statement

Callers (runner.py, sim.py, lean.py in the umbrella repo) will thread
the values from model/scorer metadata in a follow-up umbrella PR. Until
then, new rows store NULL — honest absence, not fabricated evidence.

## Tests

`tests/test_training_metadata_persistence.py` — 5 tests:
- Fresh DB has both columns
- Column migration on pre-existing table
- Values round-trip through record + SELECT
- Omitted params store NULL
- Existing caller patterns unaffected (backwards compat)
