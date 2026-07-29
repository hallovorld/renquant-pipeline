# Fail loud when the buy floor meets an uncalibrated rank_score   (PR #219)

STATUS:    delivered
WHAT:      Adds a rank_score UNIT DOMAIN marker (`RANK_SCORE_DOMAIN_RAW` at
scoring, `RANK_SCORE_DOMAIN_PROBABILITY` after calibration) and a guard in
`VetoWeakBuysTask`: if the probability-domain buy floor meets a raw-domain
rank_score, log the mismatch and `_fail_closed_panel_scoring(ctx,
"rank_score_domain_uncalibrated")` instead of silently vetoing the entire
cross-section. Absent domain (older callers that never set it) keeps the
previous behaviour.
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
domain vetoes normally, absent-domain keeps prior behaviour).
           prod or exp:   kernel correctness fix, not a model/data
performance claim — no IC/Sharpe number involved.
           existing data: `pytest tests/test_veto_quantile_floor.py` = 10/10
passed on the PR head (7 pre-existing + 3 new).
           best-known?:   n/a — bug fix, no variant comparison.
           scope:         "this is a unit-domain correctness guard, verified
by the full `test_veto_quantile_floor.py` suite above, not a
performance/model claim — §4(b) sanity triad does not apply."
NEXT:      Merge alongside `hallovorld/RenQuant#542`, then re-run the
PatchTST e2e: with the guard in place an uncalibrated swap fails loudly
instead of reporting a false "no trade".
