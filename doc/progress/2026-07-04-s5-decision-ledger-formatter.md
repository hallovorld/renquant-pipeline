# S5: Decision-ledger formatter (pipeline-side)

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
   decision records (buy/sell/hold/blocked/no_trade). Output compatible with
   `renquant_orchestrator.ledger_attribution.write_outcomes` (minus forward-return
   columns, filled later by the outcome observer).

3. **`format_rotation_decisions(ctx, config, run_id, run_date)`** — rotation-pair
   decisions with net_advantage, threshold, and executed flag.

## Wiring (umbrella-side, separate PR)

The umbrella's `RunnerAdapter.commit()` (runner.py ~L2075) needs a ~3-line
addition adjacent to the existing `record_candidate_scores()` call:

```python
from renquant_pipeline.decision_ledger import format_gate_verdicts, format_ticker_decisions
verdicts = format_gate_verdicts(ctx, self._config, run_id, run_date)
ticker_decisions = format_ticker_decisions(ctx, self._config, run_id, run_date)
```

Then pass to the orchestrator's `write_verdicts()` / `write_outcomes()`.

## Tests

18 tests covering:
- All 6 gate verdicts (regime block, conviction block, vol/wash-sale detection)
- Per-ticker decisions: buy, sell, hold, blocked, no_trade
- Mixed scenario with all decision types
- Edge cases: NaN mu, empty ctx, exit-that-is-also-a-buy dedup
- Rotation decisions with threshold comparison

## AC

- Every live run writes gate verdicts + per-ticker decisions (once umbrella
  wiring lands)
- Forward-outcome join ≥95% for aged decisions (outcome observer fills
  fwd_*_ret columns)
