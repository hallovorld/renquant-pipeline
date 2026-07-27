# Disposed-lot tax netting fix — mirror of the umbrella kernel fix   (PR #217)

STATUS:    delivered
WHAT:      `compute_disposed_lot_tax` taxed each positive-gain lot
independently and never netted losing lots within the same sell event, so
a mixed-sign multi-lot disposal (top-up lot + original lot, full exit at a
price between the two bases) produced "net loss with positive tax" —
tripping the decision-trace integrity validator `_sell_economics_are_valid`
(fail-closed RuntimeError). Fixed by netting gains/losses per rate bucket
(short-term vs long-term) within the one sell event, reusing
`compute_netted_capital_gains_tax` (same file). The validator is CORRECT
and untouched. This file is byte-identical to the fixed umbrella copy
(kernel-parity guard: `portfolio.py` is NOT allowlisted; 0 NEW drift
against the fixed umbrella branch).
WHY/DIR:   Duplicated-kernel class, triple-impl playbook pattern: fix every
copy in the same batch, then pin-sync. Pairs with the PRIMARY umbrella PR
(hallovorld/RenQuant #532, `backtesting/renquant_104/kernel/portfolio.py`
— the sim+live runtime copy). Pipeline's `_sell_economics_are_valid` copy
(`src/renquant_pipeline/kernel/persistence.py:823`) is textually identical
to the umbrella's; the same fail-close applies wherever this kernel copy
runs with persistence ON. Found via the G4 rerun batch — the first
execution of the persistence-ON validation path over a full window (the
weekly gate's `--no-persist` never exercises it).
EVIDENCE:  artifact: tests/test_disposed_lot_tax_netting.py (16 new tests,
mirrors the umbrella suite; deterministic MA 2025-06-24 reproduction: lot
+126.9676 gain taxed at 0.5->63.4838 + lot -193.2083 loss ignored ->
gross -66.2407 with tax +63.4838 pre-fix; post-fix tax=0.0, net=gross).
prod or exp: kernel correctness fix, not a model/data performance claim —
no IC/Sharpe number involved. existing data: full pipeline suite (`make
test`, deps at origin/main like CI) = 2053 passed, 2 failed, 9 skipped —
the 2 failures (`test_replay_d6_conventions.py::TestDefaultModeUnchanged`,
pin-platform byte-identity checks) fail identically on clean origin/main in
the same env (baseline 2037 passed); zero regressions, +16 new tests.
best-known?: n/a — bug fix, no variant comparison. scope: this is a
correctness fix to the duplicated kernel copy, byte-identical to the fixed
umbrella copy, verified via the full-suite run above, not a
performance/model claim.
NEXT:      Merge this mirror PR first (it is the dependency umbrella PR
#532 needs — umbrella's kernel-parity-ci compares against the
`subrepos.lock.json` pin and stays RED until this merges and the pin
advances past it), then bump the umbrella's pipeline pin through the normal
pin process.

## Additional detail — new tax semantics

Exactly `compute_netted_capital_gains_tax`'s Schedule-D shape within the
one sell event: same-bucket netting first (ST at st_rate, LT at lt_rate);
both buckets non-negative -> sum of per-bucket taxes (identical to pre-fix
for all-gain events); both non-positive -> 0 (identical for all-loss);
opposite signs -> `max(0, st_net + lt_net) *` the gaining bucket's rate.
Structurally guarantees the validator invariants: loss => tax 0, tax <=
positive gross. Reported `short_term_gross_pnl` / `long_term_gross_pnl`
splits stay pure per-bucket sums. Also fixes the sibling failure: net-GAIN
mixed sells whose per-lot tax exceeded net gross (validator invariant #4).
