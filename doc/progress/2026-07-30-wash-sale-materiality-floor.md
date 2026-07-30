# Wash-sale materiality floor: wire the live admission gate, make the threshold configurable and rollout-safe   (PR #227)

STATUS:    delivered
WHAT:      Final head (`18e4750`). All three buy-admission paths
(`task_candidates.py::WashSaleFilterTask` — the live per-ticker admission
gate that runs before the other two — `task_joint_actions.py::JointActionTask`,
`task_rotation.py::ValidatePairsTask`) resolve the materiality-floor
threshold through `kernel/selection.py::resolve_wash_sale_min_material_npv(config)`
instead of reading the config key directly. An absent, non-numeric, NaN, or
negative `wash_sale_min_material_npv` key resolves to
`WASH_SALE_MIN_MATERIAL_NPV_LEGACY = 0.0` (block on any realized loss,
byte-identical to pre-#223 behaviour) — the pipeline never substitutes its
own materiality judgement for an unconfigured strategy. An explicit
`wash_sale_min_material_npv: 1.0` in the strategy-owned config re-admits
sub-floor losses. `WashSaleFilterTask` is included because issue #223's own
measured numbers (`DROP_WashSaleFilter`, 3 of 5 sessions zeroed) came from
that task, not from the two call sites the earliest commit on this PR
touched — without it the fix was a no-op on the exact path #223 measured.
WHY/DIR:   Issue #223 found that `is_wash_sale_blocked_with_cost`'s
cost-aware branch never executes because no live caller passes
`expected_dollar_return`, so every wash-sale block is unconditional
regardless of NPV materiality ($0.04-$13.62 blocked while $6,868 cash sat
unused on 3 of 5 measured sessions). Four review rounds narrowed the fix
from a hardcoded threshold at each call site, to a configured-but-pipeline-
defaulting-to-1.0 threshold, to the final shape: the floor is a
strategy-owned policy value with a byte-identical legacy default, so
merging this PR does not silently change behaviour for any strategy config
that has not opted in.
EVIDENCE:  artifact:      tests/test_wash_sale_materiality_floor.py (24
tests: resolver-level NaN/negative/non-numeric/absent-key fallback to
LEGACY; task-level coverage of `WashSaleFilterTask`, `JointActionTask`,
`ValidatePairsTask` proving an absent key still blocks a trivial loss and
an explicit `1.0` releases it).
           prod or exp:   kernel correctness fix, not a performance claim —
no IC/Sharpe/APY number involved; §4(b) sanity triad does not apply.
           existing data: `PYTHONPATH=<renquant-common>/src <RenQuant>/.venv/bin/python3
-m pytest tests/ --tb=no -q` — `origin/main` = 1 failed / 2124 passed / 7
skipped; this branch (`18e4750`) = 1 failed / 2148 passed / 7 skipped. Same
single pre-existing failure (`test_xgboost_scorer_contract.py`, unrelated to
this change) on both; net +24 passed matches the 24 tests in the new file
exactly — zero regressions. (Earlier rounds' "50 failed" figure in the PR
thread was measured against bare system `python3` missing sibling-repo
packages on `PYTHONPATH`, not this repo's actual test health; the
venv-based comparison above is the accurate one.)
           best-known?:   n/a — bug fix, no variant comparison.
           scope:         "correctness/rollout-safety fix for the
materiality-floor default and its threading through all three live
buy-admission paths, not a model/data performance claim."
NEXT:      Two items intentionally not in this PR: (1) turning the floor on
for real requires a follow-up PR declaring `wash_sale_min_material_npv: 1.0`
in the strategy-owned config surface (`renquant-strategy-104`) with its own
pin/release, so the rollout is auditable and reversible; (2) issue #223's
second suggested item — a separate, explicitly configured policy for
"unknown realized P/L" (currently a binary block regardless of NPV
magnitude, `kernel/selection.py`'s `pl is None` branch) — is a distinct
design decision, not a materiality-floor threading gap; #223 stays open for
it.

---

## History (superseded rounds, kept for record — final head above is authoritative)

### Round 1 (`ad22ffe`) — original
Hardcoded `WASH_SALE_MIN_MATERIAL_NPV = 1.0` constant, wired only at
`task_joint_actions.py` / `task_rotation.py`. Superseded: missed
`WashSaleFilterTask` (the actual live path #223 measured) and the threshold
was not configurable.

### Round 2 (`a6baf98`) — wire the live candidate filter, make it configurable
Added `WashSaleFilterTask` (`task_candidates.py`) opt-in and switched all
three call sites to `config.get("wash_sale_min_material_npv",
WASH_SALE_MIN_MATERIAL_NPV)`. Superseded: an absent key still resolved to
the pipeline-picked `1.0`, silently changing behaviour for any strategy
config that had not declared the key.

### Round 3 (`bd97e2f`) — harden the configured floor against a config typo
Replaced the inline `float(cfg.get(key, DEFAULT))` read with
`resolve_wash_sale_min_material_npv(config)` in `kernel/selection.py`, used
by all three call sites. Failure modes measured against the real branch
(`if cost_npv < min_material_npv_cost: release`):

| configured value | effect on a $100,000 realized loss | verdict |
|---|---|---|
| non-numeric (`"1.00 USD"`, `""`) | `float()` RAISES inside a live task | crash |
| `+inf` | **UNBLOCKED** — `cost < inf` always True | **disables §1091 silently** |
| `NaN` | blocked (NaN comparison False) | fail-safe, but floor inert |
| negative | blocked | fail-safe, but floor inert |

The resolver maps all four to the DEFAULT (at this round, still
`WASH_SALE_MIN_MATERIAL_NPV = 1.0`) — treated as unset, never `0.0` and
never "no floor" — while an explicit `0.0` remains honoured as a deliberate
pre-#223 opt-out. Also fixed a docstring that contradicted the code (it
said unknown P/L was "assumed gain → not blocked" when the `pl is None`
branch returns a hard block). Superseded: the DEFAULT itself was still
`1.0`, so an unmodified strategy config still silently changed behaviour.

### Round 4 (`18e4750`) — final: stop the pipeline choosing the policy number
Renamed `WASH_SALE_MIN_MATERIAL_NPV` (`1.0`) to
`WASH_SALE_MIN_MATERIAL_NPV_LEGACY = 0.0`; the resolver now falls back to
`LEGACY` on an absent, non-numeric, NaN, or negative key, so an unmodified
strategy config is byte-identical to pre-#223. This is the head described
in the top-level WHAT/EVIDENCE above.
