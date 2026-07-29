# Fail loud when the buy floor meets an uncalibrated rank_score   (PR #219)

STATUS:    delivered
WHAT:      Adds a rank_score UNIT DOMAIN marker (`RANK_SCORE_DOMAIN_RAW` at
scoring, `RANK_SCORE_DOMAIN_PROBABILITY` after calibration) and a guard in
`VetoWeakBuysTask`: if the probability-domain buy floor meets a raw-domain
rank_score, log the mismatch and `_fail_closed_panel_scoring(ctx,
"rank_score_domain_uncalibrated")` instead of silently vetoing the entire
cross-section. Absent domain (older callers that never set it) keeps the
previous behaviour. 2026-07-29 codex review caught that only the
`score_with_history` branch stamped the RAW marker; the general branch
(PatchTST-with-history, non-history panel_ltr_xgboost, and the plain
`scorer.score(X)` fallback — includes `kind=blend`) wrote raw scores into
`cand.rank_score` with no domain stamp, so `VetoWeakBuysTask` never tripped
for those scorer kinds. Fixed by stamping `RANK_SCORE_DOMAIN_RAW` right
after that branch's scoring loop too (`job_panel_scoring.py`), mirroring
the existing early-branch stamp.
WHY/DIR:   `rank_score` is written twice — raw by the scoring stage
(`ApplyScoresTask`), calibrated probability by the calibration stage
(`ApplyGlobalCalibrationTask`). When calibration does not run, the raw value
survives into a comparison against a [0,1] floor. That is a unit error, not
a model verdict, and it presents as "no trade". The 2026-05-03 fix closed
the same confusion from the consumer side (read rank_score, not
panel_score); the uncalibrated producer path reopened it from the other
end. This is the canonical fix; `hallovorld/RenQuant#542` is a fork mirror
that keeps the umbrella's copy from diverging.
EVIDENCE:  artifact:      tests/test_veto_quantile_floor.py (3 new tests:
raw-domain fails loud with `rank_score_domain_uncalibrated`, probability-
domain vetoes normally, absent-domain keeps prior behaviour) +
tests/test_blend_scorer.py::TestKernelWiring::test_apply_scores_routes_blend_through_alpha158_raw
(extended with a domain-stamp assertion covering the general branch;
confirmed it fails with `AttributeError` on the pre-fix code, passes
post-fix).
           prod or exp:   kernel correctness fix, not a model/data
performance claim — no IC/Sharpe number involved.
           existing data: `pytest tests/test_veto_quantile_floor.py
tests/test_blend_scorer.py tests/test_gate_writers_panel_scoring.py
tests/test_active_scorer_attribution.py tests/test_panel_scoring_contract.py
tests/test_patchtst_score_collapse_guard.py
tests/test_panel_scoring_specialist_wiring.py
tests/test_patchtst_prod_telemetry_contract.py
tests/test_hf_patchtst_live_sequence.py` = 103/103 passed on the PR head
after the general-branch fix.
           best-known?:   n/a — bug fix, no variant comparison.
           scope:         "this is a unit-domain correctness guard, verified
by the full test suite above, not a performance/model claim — §4(b) sanity
triad does not apply."
NEXT:      Merge alongside `hallovorld/RenQuant#542` (same fix mirrored
there), then re-run the PatchTST e2e: with the guard in place an
uncalibrated swap fails loudly instead of reporting a false "no trade".
