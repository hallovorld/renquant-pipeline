# S5: Decision-ledger formatter + pipeline wiring

**Date:** 2026-07-04
**PR:** feat/s5-decision-ledger-formatter
**Roadmap:** S5 (decision-ledger wiring) — pipeline half

## What

### Commit 1: Formatters

Added `src/renquant_pipeline/decision_ledger.py` — a formatter that extracts
decision records from the pipeline runtime context (`ctx`) for the
orchestrator's decision ledger DB.

Three entry points:

1. **`format_gate_verdicts(ctx, config, run_id, run_date)`** — scope-level gate
   verdicts (regime, model_admission, conviction, vol_gate, wash_sale, rotation).
   Output compatible with `renquant_orchestrator.decision_ledger.write_verdicts`.

2. **`format_ticker_decisions(ctx, config, run_id, run_date)`** — per-ticker
   decision records (buy/sell/hold/blocked/no_trade). Output compatible with
   `renquant_orchestrator.ledger_attribution.write_outcomes` (minus forward-return
   columns, filled later by the outcome observer).

3. **`format_rotation_decisions(ctx, config, run_id, run_date)`** — rotation-pair
   decisions with net_advantage, threshold, and executed flag.

### Commit 2: Pipeline wiring task

Added `kernel/pipeline/task_decision_ledger.py` — `DecisionLedgerWriteTask`
that calls the formatters and writes to the orchestrator's decision ledger DB.

- Wired into `InferencePipeline.run()` at end-of-pipeline (after all decisions
  finalized, before the final summary log)
- **Fail-open**: if orchestrator modules are not importable, logs WARNING and
  continues — S5 is measurement substrate, not a trading gate
- **Default OFF**: opt-in via `decision_ledger.enabled` config flag
- Counters: `s5_verdicts_written`, `s5_decisions_formatted`, `s5_write_skipped`,
  `s5_write_error` for observability

## Tests

- 18 formatter tests (commit 1)
- 7 wiring task tests (commit 2): disabled-by-default, enabled flow, fail-open
  on import error, fail-open on write exception, run_id fallback, verdict shape

## AC

- Every live run writes gate verdicts + per-ticker decisions (when
  `decision_ledger.enabled=true` in strategy config)
- Forward-outcome join ≥95% for aged decisions (outcome observer fills
  fwd_*_ret columns — separate orchestrator job)

## Round 2 (review)

Codex found a real logic inconsistency: `_rotation_verdict()` (the scope-level
gate) counted a rotation as viable whenever `net_advantage > 0`, while
`format_rotation_decisions()`'s per-rotation `executed` field used the actual
economic bar, `net_advantage >= threshold`. A run could report the rotation
gate as `allow` with a positive `n_viable` count even though every individual
rotation was below threshold and none would execute — two conflicting stories
for the same run.

Fixed by making `_rotation_verdict()` use the identical `net_advantage >=
threshold` predicate as `format_rotation_decisions()`. No downstream consumer
of the gate verdict distinguishes "positive but sub-threshold" from
"executable" (this PR is unmerged, so there are no external consumers yet
either), so a single shared predicate is the correct fix rather than carrying
two parallel signals.

Added `test_rotation_verdict_matches_per_rotation_executed_flag` (rotations
with `net_advantage > 0` but all below `threshold` — asserts the gate reports
`halve`/`n_viable == 0`, consistent with `format_rotation_decisions()`
reporting no rotation as executed) and
`test_rotation_verdict_allows_when_above_threshold` (control case). Confirmed
the new regression test fails against the pre-fix code (`allow` instead of
`halve`) and passes after. 20/20 decision_ledger tests pass; full repo suite
1310/1314 passes (4 pre-existing failures — a stale sibling `renquant-base-data`
checkout on this machine plus one already-failing HF live-sequence test —
confirmed reproducing identically on a clean `origin/main` checkout, unrelated
to this change).
