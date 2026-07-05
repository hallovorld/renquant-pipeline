# S5: Decision-ledger formatter (pipeline-side, verdict-only wiring)

**Date:** 2026-07-04
**PR:** feat/s5-decision-ledger-formatter
**Roadmap:** S5 (decision-ledger wiring) — pipeline half

## What

Added `src/renquant_pipeline/decision_ledger.py` — a formatter that extracts
decision records from the pipeline runtime context (`ctx`) for the
orchestrator's decision ledger DB.

Three entry points:

1. **`format_gate_verdicts(ctx, config, run_id, run_date)`** — scope-level gate
   verdicts (regime, model_admission, conviction, vol_gate, wash_sale, rotation).
   Output compatible with `renquant_orchestrator.decision_ledger.write_verdicts`.

2. **`format_ticker_decisions(ctx, config, run_id, run_date)`** — per-ticker
   decision records (buy/sell/hold/blocked/no_trade). Formatted for
   observability only in this PR — see Round 2 below for why this is not yet
   persisted.

3. **`format_rotation_decisions(ctx, config, run_id, run_date)`** — rotation-pair
   decisions with net_advantage, threshold, and executed flag.

Added `kernel/pipeline/task_decision_ledger.py` — `DecisionLedgerWriteTask`
that calls the formatters and writes **verdicts only** to the orchestrator's
decision ledger DB.

- Wired into `InferencePipeline.run()` at end-of-pipeline (after all decisions
  finalized, before the final summary log)
- **Fail-open**: if orchestrator modules are not importable, logs WARNING and
  continues — S5 is measurement substrate, not a trading gate
- **Default OFF**: opt-in via `decision_ledger.enabled` config flag
- Counters: `s5_verdicts_written`, `s5_decisions_formatted`, `s5_write_skipped`,
  `s5_write_error` for observability

## Tests

- 18 formatter tests
- 7 wiring task tests: disabled-by-default, enabled flow, fail-open
  on import error, fail-open on write exception, run_id fallback, verdict shape

## AC

- Every live run writes gate verdicts (when `decision_ledger.enabled=true` in
  strategy config)
- Per-ticker decision persistence is NOT yet delivered — see Round 3.

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

## Round 3 (review — this round)

Codex found a concrete implementation gap: the task formats both gate verdicts
and per-ticker decisions, but only persists verdicts — `decisions` is counted/
logged, never written anywhere. That means the PR does not deliver the
per-ticker decision-ledger substrate its earlier framing implied.

Investigated whether the "correct persistence API" codex asked for actually
exists and is safely callable. It does exist in shape:
`decision_outcomes` (in `renquant_orchestrator/ledger_attribution.py`) has a
`ticker` column, and `write_outcomes()` accepts rows with the forward-return
columns left `None`. On the surface this looks like exactly what
`format_ticker_decisions()`'s output should feed.

**But calling it from this task would be unsafe.** `outcome_observer.py`
(PR #351, merged) established that `decision_outcomes` rows must be written
ATOMICALLY — once per decision, only after all three horizons (5d/20d/60d) are
available — specifically to prevent a partial-row poisoning bug: because
`pending_decisions()`'s join checks row EXISTENCE at `(as_of, scope, gate)`
grain (no ticker in the join condition — decision_ledger has no ticker
dimension), any row present there is treated as "already observed" regardless
of whether its forward-return columns are populated. If this pipeline task
wrote a verdict-only row into `decision_outcomes` at pipeline-run time (well
before the 60d aging window), the S5 outcome observer would forever skip that
`(as_of, scope, gate)` for real forward-return backfill — the exact bug #351
just fixed, reintroduced via a different write path.

**Resolution**: kept this task verdict-only (its actual behavior was already
correct; only the framing overclaimed). Rewrote the module/class docstrings to
state this explicitly, with the poisoning mechanism spelled out, and fixed the
success-path log line (previously "wrote N verdicts, M decisions" — implied
decisions were written too). Narrowed this doc's AC section to verdict-only.
Per-ticker decision-ledger persistence needs a separate registry table the
observer can read FROM (distinct from `decision_outcomes`, which the observer
must remain the sole writer of) — that is follow-up design work tracked
separately, not a missing API call in this task.

No code behavior changed (the task already didn't write decisions); this round
is a documentation-accuracy fix plus a regression test guarding against ever
wiring the unsafe path.
