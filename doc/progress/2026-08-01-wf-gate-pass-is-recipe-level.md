# GOAL-6 — 40 artifacts are stamped "WF gate passed" and not one of them was scored

**Date:** 2026-08-01 · `renquant-pipeline` · GOAL-6 (evaluation path)

## The measurement

Across every stamped artifact under `RenQuant/backtesting/renquant_104/artifacts`
`[本次实测 2026-08-01]`:

| | |
|---|---:|
| artifacts carrying a `wf_gate_metadata` block | **53** |
| with `artifact_usage.candidate_artifact_used = false` | **53 / 53** |
| …of those, carrying `passed = true` | **40** |
| sharing the single recipe fingerprint `sha256:cfdd6cb8e950da0f` | **51 / 53** |
| `manifest_rows_checked` on every stamped one | **43** |

**Not one artifact in the tree has had its own booster evaluated by the gate that stamped
it.** The 40 passes are 40 copies of one assertion about one recipe.

## The producer is honest; the consumer discarded the qualifier

`RenQuant/scripts/run_wf_gate.py::inspect_artifact_usage` returns
`candidate_artifact_used=False` **unconditionally** when the strategy config uses a
walk-forward manifest, and says why in its own docstring: *"A walk-forward manifest
validates a retraining recipe / manifest instead; it must not silently stamp the candidate
artifact as passed."* The candidate-scoring branch (`eval_scope="static_artifact"`) is
reachable only when `walkforward.enabled` is false — which is why it has run **0 times** on
this surface. That is by construction, not by accident, and it is correctly recorded.

The defect is downstream. `kernel/preflight.py::_check_wf_gate_metadata` — **P-WF-GATE,
the production trust boundary for admitting buys** — read `wf.get("passed")` and nothing
else. The one field that says *what the pass is a statement about* existed in the data,
was written deliberately, and never reached the check or the run bundle.

## What this change does, and deliberately does not

**Surfaces. Does not enforce.** `gate_evidence_scope` (`artifact` / `recipe` / `unstated`)
now appears in `details` on every path. **Admission is unchanged**, and four
parametrised tests assert `(ok, severity)` is identical across every `artifact_usage`
shape including a malformed one.

That restraint is the point: **40 of 40 passing artifacts carry `candidate_artifact_used
= false`**, so failing on it would block new buys on every artifact in the tree at once.
The fix-wave rule exists because a compliance change that stops order placement is worse
than the gap it closes. The policy question — *may a recipe-level pass admit capital?* — is
now answerable from the bundle instead of invisible, and it is the operator's to answer.

`unstated` is a third value, not a synonym for either: a missing or non-boolean
`candidate_artifact_used` has not established that the artifact went unscored any more than
that it was scored. `0` and `1` are the trap that makes this a type check rather than a
truthiness check, and `artifact_usage` arriving as a **string** is the `(x or {}).get(...)`
shape that has produced four separate defects in this programme — so the type is checked,
not assumed.

## What is NOT claimed

That the gate design is wrong — validating a recipe across 43 walk-forward folds is a real
check, and the producer never claimed otherwise. That any of the 40 artifacts is bad. That
`sanity_regime_ic` is affected. This establishes **what the stamp means**, and makes the
consumer say it.

## Tests

22. The load-bearing four are the invariance ones. Suite: **2252 passed, 7 skipped, 1
failed** — the failure is
`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
**verified to pre-exist** by running it on a detached `origin/main` worktree, where it
fails identically. Verified, not assumed.
