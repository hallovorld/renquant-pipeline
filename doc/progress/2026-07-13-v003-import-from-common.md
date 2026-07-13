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
