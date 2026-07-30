# Wash-sale materiality floor: wire the live admission gate, make the threshold configurable   (PR #227)

STATUS:    delivered
WHAT:      Fix-round on top of the original commit (`ad22ffe`), addressing
two CHANGES_REQUESTED reviews. (1) `WashSaleFilterTask`
(`kernel/pipeline/task_candidates.py`) — the actual live per-ticker
admission gate that runs before `task_joint_actions.py` /
`task_rotation.py` — now opts into `WASH_SALE_MIN_MATERIAL_NPV` too. A
ticker it drops never reaches the two call sites the original commit wired,
so without this the fix was a no-op on the exact path issue #223 measured
(`DROP_WashSaleFilter`). (2) The floor is no longer a hardcoded constant at
each call site: all three buy-admission paths
(`task_candidates.py::WashSaleFilterTask`,
`task_joint_actions.py::JointActionTask`,
`task_rotation.py::ValidatePairsTask`) now read
`config.get("wash_sale_min_material_npv", WASH_SALE_MIN_MATERIAL_NPV)`, so
a deployment can override the $1.00 default without a code change, while an
absent key reproduces this PR's original behaviour exactly (backward
compatible). Also fixed a pre-existing indentation defect in
`task_rotation.py`'s wash-sale import block (touched by this same edit) and
added 5 task-level tests exercising all three call sites end to end —
codex's review noted the original 10 tests only exercised the bare
function, not any task.
WHY/DIR:   Issue #223's own numbers (`DROP_WashSaleFilter`, 3 of 5 sessions
zeroed) came from `WashSaleFilterTask`, not from the two call sites the
original commit touched — codex's HIGH finding on the first review round.
The second review asked for a configured threshold instead of a hardcoded
one, so a deployment can tune materiality without a code change, and for
this PR to stop overstating its relationship to #223.
EVIDENCE:  artifact:      tests/test_wash_sale_materiality_floor.py (19
tests: 9 original function-level + 5 new task-level: default floor applies
without config in `WashSaleFilterTask` / `ValidatePairsTask` /
`JointActionTask`, a configured `wash_sale_min_material_npv=0.0` restores
the unconditional block in all three, a configured higher floor waves
through a material-by-default loss).
           prod or exp:   kernel correctness fix, not a performance claim —
no IC/Sharpe/APY number involved; §4(b) sanity triad does not apply.
           existing data: `PYTHONPATH=.../renquant-common/src python3 -m
pytest tests/ --tb=no -q` on this head = 50 failed / 2081 passed / 9
skipped, against the PR's own quoted baseline of 50 failed / 2076 passed on
`ad22ffe` (same pre-existing 50 failures, unrelated to this change) — net
+5 passed, matching the 5 new tests, zero regressions.
           best-known?:   n/a — bug fix, no variant comparison.
           scope:         "unit-level and task-level correctness tests for
a materiality-floor threading fix, not a model/data performance claim."
NEXT:      Not in scope for this fix round, by the reviewer's own stated
alternative: issue #223 also asks for a separate, explicitly configured
policy for "unknown realized P/L" (currently a binary block regardless of
NPV magnitude — `kernel/selection.py`'s "P/L data not available ... Fall
back to binary block" branch). That is a distinct design decision (its own
classification, its own config surface, its own tests) rather than a
materiality-floor threading gap, so it is intentionally NOT done here;
issue #223 should stay open for it and PR #227 does not close it.
