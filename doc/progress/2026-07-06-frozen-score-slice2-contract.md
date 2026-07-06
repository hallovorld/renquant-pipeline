# Frozen-score diagnostic contract (slice-2, for orchestrator consumption)

**Date:** 2026-07-06
**PR:** feat/frozen-score-slice2-contract
**Roadmap:** RFC #208 §8 row 2 (pipeline slice) — architecture-boundary fix

## What

Added `FrozenScoreScoringJob` to `src/renquant_pipeline/panel_scoring.py` and
`run_frozen_score_diagnostic_tick()` / `frozen_score_diagnostic_stages()` to
`src/renquant_pipeline/intraday_decisioning.py`.

This moves the frozen-score diagnostic composition (previously built
directly inside `renquant-orchestrator`'s `intraday_session_scheduler.py`,
importing `panel_scoring` tasks and `SelectionJob` to hand-assemble a custom
stage list) into the repo that owns pipeline/signal internals — the same
class of fix as the round-1 broker-adapter relocation on
`renquant-orchestrator#400`, this time for the `AGENTS.md` "no signal/decision
internals in orchestrator" boundary. Codex round 2 on that PR: `bind_pipeline_tick_runner()`
"is no longer just consuming the intraday decisioning contract; it is
reaching into pipeline internals and redefining stage composition here."

`FrozenScoreScoringJob(PanelScoringJob)` swaps `LoadScorerTask` +
`BuildFeatureMatrixTask` for `_StubFrozenFeatureMatrixTask` (injects an
empty feature matrix keyed by the frozen class-A scores, with a
`default_quantity=1` fallback), keeping `ApplyScoresTask` →
`ApplyGlobalCalibrationTask` → `RegimeModelAdmissionTask` →
`VetoWeakBuysTask` (+ optional `EmitAttributedOrderIntentsTask`) unchanged —
the same gate stack the real path runs, downstream of feature availability.
`run_frozen_score_diagnostic_tick()` has the same signature as
`run_intraday_decision_tick()` minus the `stages=` parameter (removing the
external escape hatch that let a caller inject arbitrary stage composition)
and internally drives `[FrozenScoreScoringJob(), SelectionJob(),
FrozenScoreScoringJob(emit_orders=True)]`.

**A first attempt at `FrozenScoreScoringJob(Job)` (standalone, not inheriting
`PanelScoringJob`) broke `tests/test_gate_writers_panel_scoring.py
::TestCensusPin::test_single_designated_writer`** — an AST-level census
pinning exactly one `setattr(ctx, "buy_blocked", True)` writer in this file
(errata-C(iii) choke point). Copy-pasting `PanelScoringJob.run()`'s body
created a second writer. Fixed by inheriting from `PanelScoringJob` and
overriding only `__init__`/`tasks`, so `run()`/`should_skip()` are reused,
not duplicated.

## DIAGNOSTIC / DEBUG PROBE ONLY — carried over from the orchestrator PR

Not a validated intent-generation design: no proof that bypassing the
feature contract preserves the pipeline's real semantics, and no
exit/sell path. See both classes' docstrings for the full caveat, including
one correction found while writing the tests below: the "quantity is always
1 regardless of price/risk" claim depends on the caller never populating
`market_snapshot["order_quantity_by_ticker"]` — true of the one real caller
today (orchestrator's `session_start_provider` builds no real-time quantity
map), but not an intrinsic property of the job itself. Docstrings now say
this precisely.

## Tests

5 new tests in `tests/test_intraday_decisioning.py`:
- `test_frozen_score_diagnostic_stages_uses_frozen_score_scoring_job` —
  stage-list shape.
- `test_frozen_score_diagnostic_tick_emits_from_frozen_signal_not_features` —
  a below-buy-floor frozen score (IBM, 0.2 vs 0.5 floor) is still blocked
  through the real gate stack; admitted names match the frozen scores.
- `test_frozen_score_diagnostic_tick_has_no_real_sizing_control` — modeling
  the real caller's actual gate-input shape (no `order_quantity_by_ticker`),
  every admitted intent sizes at the fallback quantity of 1.
- `test_frozen_score_diagnostic_tick_still_enforces_leak_guard` — the §6
  class-A leak guard still fires; this is not a parallel unguarded path.
- `test_frozen_score_diagnostic_tick_is_deterministic`.

Full suite: 1336 passed, 7 skipped, 0 failures (confirmed no regression from
either the new job or the inheritance fix).

## AC

- `renquant-orchestrator`'s `intraday_session_scheduler.py` imports only
  `renquant_pipeline.intraday_decisioning` — zero direct references to
  `panel_scoring`/`selection` internals (grep-confirmed in the companion PR).
- The `buy_blocked` single-writer census pin still holds (exactly 1).
- Frozen-score semantics are now test-proven, not just docstring-asserted.

## NEXT

Land this PR, then bump/confirm `renquant-orchestrator`'s local dev checkout
picks it up (loose `renquant-pipeline>=0.1.0` lower bound + sibling-checkout
PYTHONPATH — no pin bump required for local testing). The unproven semantic-
validity question (does the empty-feature-matrix bypass produce meaningful,
non-degenerate intents in a real session, not just the synthetic test
fixture here?) remains open and is explicitly out of scope for this PR,
which only relocates the composition to the correct repo.
