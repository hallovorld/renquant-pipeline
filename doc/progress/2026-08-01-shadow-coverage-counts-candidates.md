# coverage_frac measures the candidate set again (GOAL-1, orch#727)

## The defect `[早前实测 2026-08-01, orch#727]`

`coverage_frac = n_scored / n_candidates` used a numerator counted over the SHADOW'S
OWN matrix (`len(_finite)` over `scorer.score(sub)`) and a denominator counted over the
primary's candidates (`len(primary_scores)`). The clf lane scores 322 names against 292
candidates, so the "fraction" exceeded 1.0 on **every session since go-live** — 12/12
records fault, peak **1.1039** — and the health record's stated intent ("coverage of
the candidate cross-section by finite shadow scores") was never what it computed. Same
failure shape as [guards-that-validate-the-wrong-object]: the check's subject was not
the object it claimed.

## The fix

Numerator = `set(primary_scores) ∩ finite(shadow_scores)`. The shadow's raw breadth
stays observable as a NEW field `n_scored_total` (seeded 0 alongside the other schema
fields), so "scored wider than the candidates" remains visible without breaking the
fraction. A shadow that scores ONLY non-candidates now records `n_scored=0,
coverage_frac=0.0` and faults via the existing zero-scored path — previously it would
have looked like 67% "coverage".

The orchestrator sentinel's `> 1.0` ceiling finding (landed separately) stays as the
guard against regression; after this fix it should never fire.

## Tests `[本次实测 2026-08-01]`

+2: wider-matrix shadow → `n_scored=3, n_scored_total=5, coverage_frac=1.0`;
non-candidate-only shadow → `0 / 0.0 / fault`. Shadow-adjacent files: 46 passed. Full
suite: **2300 passed, 1 failed, 8 skipped** — the failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
`InferenceContext has no attribute accepted_candidates`) is PRE-EXISTING: identical on
the untouched checkout at `a14dad1` without this branch's changes.

## Deployment note

Merged ≠ live: the daily run consumes the PINNED runtime copy; this reaches the live
health records only when the operator syncs the pipeline pin.
