# Progress — disposed-lot tax netting fix (mirror of the umbrella kernel fix)

**Date:** 2026-07-27. **Type:** accounting bug fix in the duplicated kernel
copy. **Pairs with:** the PRIMARY umbrella PR (hallovorld/RenQuant,
`backtesting/renquant_104/kernel/portfolio.py`) — same function, two
copies (duplicated-kernel class; triple-impl playbook pattern: fix every
copy in the same batch, then pin-sync).

## STATUS
delivered (PR open)

## BOTTOM LINE
`compute_disposed_lot_tax` taxed each positive-gain lot independently and
never netted losing lots within the same sell event, so a mixed-sign
multi-lot disposal (top-up lot + original lot, full exit at a price between
the two bases) produced "net loss with positive tax" — tripping the
decision-trace integrity validator `_sell_economics_are_valid`
(fail-closed RuntimeError). Fix: net gains/losses per rate bucket within
the one sell event by reusing `compute_netted_capital_gains_tax` (same
file). The validator is CORRECT and untouched. This file is byte-identical
to the fixed umbrella copy (kernel-parity guard: portfolio.py is NOT
allowlisted; 0 NEW drift against the fixed umbrella branch).

## VERIFIED INSTANCE [VERIFIED]
MA 2025-06-24 sim sell: lot +126.9676 gain (taxed 0.5 → 63.4838) + lot
−193.2083 loss (ignored) → gross −66.2407 with tax +63.4838. Found by the
G4 rerun batch — first execution of the persistence-ON validation path over
a full window (the weekly gate's `--no-persist` never exercises it).
Pipeline's `_sell_economics_are_valid` copy
(`src/renquant_pipeline/kernel/persistence.py:823`) is textually identical
to the umbrella's; the same fail-close applies wherever this kernel copy
runs with persistence ON.

## NEW TAX SEMANTICS
Exactly `compute_netted_capital_gains_tax`'s Schedule-D shape within the
one sell event: same-bucket netting first (ST at st_rate, LT at lt_rate);
both buckets non-negative → sum of per-bucket taxes (identical to pre-fix
for all-gain events); both non-positive → 0 (identical for all-loss);
opposite signs → `max(0, st_net + lt_net) ×` the gaining bucket's rate.
Structurally guarantees the validator invariants: loss ⇒ tax 0, tax ≤
positive gross. Reported `short_term_gross_pnl` / `long_term_gross_pnl`
splits stay pure per-bucket sums. Also fixes the sibling failure: net-GAIN
mixed sells whose per-lot tax exceeded net gross (validator invariant #4).

## TESTS [VERIFIED]
- NEW `tests/test_disposed_lot_tax_netting.py` (16 tests, mirrors the
  umbrella suite): exact MA lot pair (tax MUST be 0.0, net = gross);
  pre-fix triple fails the validator; net-gain mixed case (tax = 0.5 ×
  netted sum, within the invariant-#4 bound); all-gain/all-loss regression;
  ST/LT bucket-rate separation; cross-bucket cases asserted EQUAL to
  `compute_netted_capital_gains_tax`; validator run directly on fixed
  outputs.
- Full pipeline suite (`make test`, deps at origin/main like CI):
  **2053 passed, 2 failed, 9 skipped** — the 2 failures
  (`test_replay_d6_conventions.py::TestDefaultModeUnchanged`) fail
  identically on clean origin/main in the same env (pre-existing,
  pin-platform byte-identity checks; baseline 2037 passed). Zero
  regressions; +16 new tests.
