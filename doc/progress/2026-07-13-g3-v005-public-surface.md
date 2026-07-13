# fix(V-005): add public re-export surface for cross-repo consumers

**Date**: 2026-07-13
**PR**: pipeline fix/v005-public-surface (#197)

## Change

Add `renquant_pipeline.public` as a narrow, lazy re-export surface for the
types/operations that the orchestrator's `native_context_hydration.py`
demonstrably needs from kernel internals. Went through three rounds:

1. **Round 1 (rejected, codex)**: eager catch-all re-exporting `LocalStore`,
   `last_completed_nyse_session`, `HoldingState`, `RegimeState`,
   `LoadUniverseJob`, `UniverseContext`, `record_training_run` together.
   Codex: don't eagerly import unrelated kernel subsystems for one type;
   `LoadUniverseJob`/`UniverseContext` are execution internals, not a
   contract; `record_training_run`'s only consumer (`train_gbdt.py`) is
   itself an ownership violation (out of scope — leave alone); the calendar
   helper already has a canonical home in `renquant_common.market_calendar`.
2. **Round 2 (narrowed, codex CHANGES_REQUESTED)**: dropped Job/Context,
   training persistence, and the calendar helper; kept `LocalStore`,
   `HoldingState`, `RegimeState` behind lazy `__getattr__` (module import
   loads zero kernel modules). Codex called the shape "the right
   correction" but flagged the subprocess import-surface tests: they used
   `pytest.skip(...)` on child-process failure, which could certify a
   broken contract as skipped.
3. **Round 3 (this pass)**: hardened `tests/test_public_surface.py` — child
   subprocess failures are now hard assertions (never skipped), failure
   messages include stderr + exit code, and negative cross-contamination
   assertions prove `LocalStore` access does not load `kernel.exits`/
   `kernel.regime` (and vice versa for `HoldingState`/`RegimeState`). No
   `pytest.skip` remains anywhere in the file — matches the hard-assert
   pattern in orchestrator's `tests/test_import_boundaries.py`.

   Also added the narrow, pipeline-OWNED **operation** orchestrator#513
   needs in place of constructing `LoadUniverseJob`/`UniverseContext`
   directly (codex round-1 point 2): `public.load_universe(*, config,
   strategy_dir, broker_name=None, held_tickers=None, as_of_date=None) ->
   UniverseLoadResult`. It runs the real `LoadUniverseJob` chain (artifact
   load → staleness filter → universe-floor filter → auto-drop filter)
   against a fresh `UniverseContext` it builds and tears down internally,
   returning only `models`/`rejections` — never the Job/Context objects.
   The kernel import is function-scoped: referencing/importing
   `load_universe` loads no kernel module; only calling it does (proven by
   `test_load_universe_import_is_lazy_until_called`). Consumer-contract
   tests cover the admitted/rejected-artifact path and the
   held-tickers-is-authoritative-even-when-empty semantics.

Public exports: `LocalStore`, `HoldingState`, `RegimeState`,
`UniverseLoadResult`, `load_universe`.

## Motivation

V-005 (architecture audit): orchestrator imports pipeline kernel
internals at several call sites, creating fragile coupling to internal
module layout and, for `LoadUniverseJob`/`UniverseContext`, promoting a
pipeline execution internal to a permanent cross-repo contract by
construction. The narrow surface decouples orchestrator from kernel
module layout while keeping Job/Context lifecycle pipeline-owned.
