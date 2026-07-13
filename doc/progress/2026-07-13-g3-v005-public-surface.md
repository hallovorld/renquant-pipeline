# fix(V-005): add public re-export surface for cross-repo consumers

**Date**: 2026-07-13
**PR**: pipeline fix/v005-public-surface

## Change

Add `renquant_pipeline.public` as a stable re-export surface for types
and functions that sibling repos (orchestrator) need from kernel
internals.  This is step 1 of V-005 remediation: orchestrator will
switch its 6 `from renquant_pipeline.kernel.*` imports to use this
module instead.

Re-exported symbols: `LocalStore`, `last_completed_nyse_session`,
`HoldingState`, `RegimeState`, `LoadUniverseJob`, `UniverseContext`,
`record_training_run`.

## Motivation

V-005 (architecture audit): orchestrator imports pipeline kernel
internals at 6 call sites in 2 files, creating fragile coupling to
internal module layout.  Any kernel refactor can silently break the
orchestrator at a layer pipeline's own tests wouldn't catch.
