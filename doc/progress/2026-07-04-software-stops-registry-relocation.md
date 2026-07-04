# Software-stop registry relocated here from the RenQuant umbrella

DATE: 2026-07-04
SCOPE: renquant-pipeline (new module `software_stops.py`)
DESIGN: renquant-orchestrator `doc/design/2026-07-02-s-frac-fractional-v2.md`
§3.2 (registry + sell-only-loop delta) / §3.3 (failure modes) / §3.4
(staleness watchdog).
CONSUMED CONTRACTS: none new — this is a pure relocation, byte-identical
behavior to the umbrella's original `adapters/software_stops.py`.

## What

Moved `SoftwareStopRegistry` (+ `compute_staleness`, `registry_path_for`,
`SoftwareStopRegistryCorrupt`, `DEFAULT_MAX_STALENESS_MINUTES`) here from
`RenQuant`'s `backtesting/renquant_104/adapters/software_stops.py`. Companion
fix to `RenQuant#440`'s round-5 review — codex's repeated finding across 4
rounds was that new capability logic (a ~589-line stop-loss registry +
evaluator) belongs in an owning repo by default, not the umbrella, regardless
of whether a duplicate already exists elsewhere to point to.

## Why this repo, not renquant-execution

Investigated both candidates directly rather than guessing:

- The registry's only external dependency (`kernel.state_paths._safe_broker`,
  for broker-isolated file paths) already exists byte-identically in this
  repo's `renquant_pipeline.kernel.state_paths` (the Phase 1 mirror) — a
  ready-made, zero-new-wiring home.
- `renquant_pipeline` is ALREADY an established, working cross-repo import
  source for the umbrella's live-runner tree (e.g. `adapters/runner.py`
  imports `renquant_pipeline.kernel.gate_registry.ctx_registry`) — the same
  lazy, function-local import pattern (`# noqa: PLC0415`) the umbrella's
  `adapters/runner.py:175` already used for the old local
  `adapters.software_stops` import.
- By contrast, nothing in the umbrella's live-runner tree currently imports
  `renquant_execution` as an installed package at all — routing there would
  be first-of-its-kind wiring, not an established pattern.

## Test split

`tests/test_software_stops.py` (this repo, companion to
`RenQuant#440`/`renquant-pipeline#165`'s mirrored
`kernel/pipeline/task_software_stops.py`) already carried
`SoftwareStopExitTask` wiring tests against a FAKE duck-typed registry — kept
as-is (the task never imports the registry class directly). Added the
registry's own unit-test coverage from the umbrella's original test file:
registry round-trip, ratchet-only invariant, trigger correctness, gap-through
pricing, corruption fail-closed (4 of 5 methods — the stage-0
capability-gate integration test stays in the umbrella, since it exercises
umbrella-only `RunnerAdapter`/`FakeBroker` orchestration), and staleness
watchdog arithmetic (3 of 4 methods — the ops watchdog CLI script's own
exit-code test also stays in the umbrella, testing that script directly).

29/29 new + existing tests in this file pass; 1249/1249 full repo suite
passes (7 pre-existing skips).

## What the umbrella side is reduced to

See the companion RenQuant#440 progress doc (round 5) for the umbrella-side
changes: `adapters/software_stops.py` removed entirely, `adapters/runner.py`
and `scripts/check_software_stops_liveness.py` updated to import from
`renquant_pipeline.software_stops` directly.
