# Land a measured, not-yet-validated holiday-aware trading-day bound   (PR #229)

STATUS:    delivered
WHAT:      `leakage_guard.py`'s boundary comment promised a "calendar days,
conservative upper bound" but the code used `BDay(lookahead_days)`, which
counts business days and does not skip market holidays — for a
TRADING-day label (e.g. `fwd_60d_excess`) that lands short. This PR adds
`_measured_trading_day_calendar_bound`, a module-private helper derived
from measuring SPY's real trading dates 2016-01-04 → 2026-07-29 (2,597
cutoffs), plus 6 tests pinning both the measurement and the deliberate
non-wiring. The production call site (`assert_no_leakage`) is **not**
switched to it — `tests/test_wf_fold_selection_parity.py::
test_newest_fold_inside_embargo_window_older_wins` fails under the
corrected bound, proving the switch moves which model gets promoted,
which #228's acceptance criteria require an A/B for.
           2026-07-30 codex review (CHANGES_REQUESTED) caught two scoping
problems, both fixed in this pass: (P1) the helper was public
(`trading_days_to_calendar_bound`) and its docstring read as a general
"CONSERVATIVE" guarantee, but the 4-days-per-20-trading-days allowance is
fitted to one sample and the function takes no exchange-calendar /
instrument / validity-window argument — nothing established it stays
conservative for a future holiday cluster or a non-US corpus. (P2) the PR
title ("fix(wf): land the holiday-aware trading-day bound") implied a
deployable remediation that does not yet exist. Fix: renamed the helper
(and its backing constant) to a module-private, explicitly-scoped name
(`_measured_trading_day_calendar_bound`), rewrote its docstring to state
plainly that it is a research measurement pending a real per-corpus
trading-calendar input (tracked in #228) and is consumed only by this
module's own tests — not exported as a correctness primitive — and
retitled the PR to `feat(wf): land a measured, not-yet-validated
holiday-aware trading-day bound (#228)`.
WHY/DIR:   Part of the #228 leakage-guard audit (§7.2/§7.13 discipline):
every leakage-relevant bound must be provably conservative or explicitly
labelled as unproven, never asserted past what was actually measured.
Demoting the helper to private + measurement-scoped closes that gap
without expanding this PR's surface into building a real trading-calendar
integration (out of scope here; tracked in #228).
EVIDENCE:  artifact:      tests/test_leakage_guard_trading_day_bound.py
(6 tests, all updated to import the renamed private symbol) +
tests/test_wf_fold_selection_parity.py (29 tests, unaffected — call site
untouched).
           prod or exp:   experiment/measurement primitive, module-private,
not wired into any production path (`grep -rn
"_measured_trading_day_calendar_bound" src tests` shows zero call sites
outside its own definition and its own test file).
           existing data: `pytest tests/test_leakage_guard_trading_day_bound.py
tests/test_wf_fold_selection_parity.py -q` → 35 passed, 0 failed, on this
branch after the rename.
           best-known?:   n/a — correctness-scoping fix, not a
model/data performance claim.
           scope:         "this is a docstring/naming correction on an
unwired, module-private measurement helper, verified by the two test
files above; it does not change `assert_no_leakage`'s behaviour, so the
PR's own before/after suite parity (50 failed / 2097 passed vs baseline
50 failed / 2091 passed) still holds."
NEXT:      #228 remains open for the other known-short call sites
(`wf_gate/runner.py:1949,1986`, `train_walkforward_panel.py:237`,
`fit_walkforward_calibrators.py:48`, `meta_label/purged_kfold.py:122`,
plus `renquant-model`'s `panel_data.py` and the PatchTST pipeline) and for
building the real per-corpus trading-calendar input this measurement is
a placeholder for.
