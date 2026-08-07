# score_drift: score the candidate population, not a mixture of two units

STATUS:    Implemented, 12 tests green in the touched file, full suite 2545 passed.
           Does NOT reduce the standing CRITICAL — see EVIDENCE.

WHAT:      `load_score_drift_from_db` now restricts to `role='candidate'` (plus
           `role IS NULL` for pre-role-column rows), and probes for the column so
           a DB without it degrades to the old pooled behaviour instead of
           raising `OperationalError`.

WHY/DIR:   `candidate_scores` holds two populations whose `rank_score` is not the
           same quantity. Within ONE live run, measured 2026-08-07:

             candidate  n=84  [-2.667, 3.050]   the scorer's z-composite
             holding    n=10  [ 0.104, 0.340]   calibrate_probability(panel_score)

           `ApplyGlobalCalibrationTask` runs after `ApplyScoresTask` and writes
           the hold side as a bounded probability on every bar, by design. So the
           pooled PSI is computed over a mixture of two incommensurable units,
           which is not a distribution statistic about anything.

           `panel_score` was considered as the common column instead and
           REJECTED: it is NULL on 93% of holding rows (59 of 818 non-null since
           08-04), so switching to it would silently drop nearly every holding
           while appearing to unify the scale.

EVIDENCE:  artifact:      `tests/test_score_drift_monitor.py::TestOnlyCandidateRowsAreScored`
                          (3 cases) + `test_a_db_without_the_role_column_still_works`
           prod-or-exp:   prod kernel, monitoring path only; emits no orders
           existing-data: `python3 -m pytest tests/ -q` -> 2545 passed, 9 skipped,
                          2 failed. Both failures are
                          `test_replay_d6_conventions.py::TestDefaultModeUnchanged`,
                          which reproduce on unmodified `origin/main` and are a
                          LOCAL environment artifact: `run_ab_replay` swallows
                          `ImportError` and leaves `hac_t_stat=None` because this
                          venv lacks `statsmodels`. CI has it and is green.
                          `[VERIFIED — pytest, 2026-08-07]`
           best-known?:   yes for the stated goal (one population per statistic).
                          It is NOT a fix for the standing CRITICAL: on the live
                          DB the same window moves **3.5956 -> 4.6600** once
                          holdings are dropped, because the probability-scale rows
                          were DILUTING the z-scale ones, not inflating them.
                          `[VERIFIED — psi() under pytest so the repo pythonpath
                          applies, 2026-08-07]`
           scope:         also drops ONE run from the 95 that previously counted
                          as "full" — a bar that cleared `MIN_SCORES_PER_RUN`
                          only because holding rows padded it past 30.
                          `[VERIFIED — same probe]`

           Reverse check that the guards are not vacuous: with the filter removed,
           all three `TestOnlyCandidateRowsAreScored` cases fail and the rest pass.
           `test_the_exclusion_is_by_ROLE_not_by_value` puts holdings on the SAME
           scale as candidates and still requires them dropped, so the suite
           cannot pass on a filter that merely trims outliers.

NEXT:      1. The CRITICAL band is unexplained by this change and remains open.
              The 08-04 unit change is still the leading account; the trailing-20
              window should flush as pre-08-04 runs age out, checkable 08-11/12.
           2. NOT DONE: whether candidates and holdings SHOULD share one scale is
              a design question for whoever made the 08-04 z-blend call — the
              candidate path stopped being calibrated then while the hold path did
              not. That is orch#900, which I closed as not-a-defect in its
              original framing; the scale asymmetry itself is still unadjudicated.
           3. `active_scorer` is NULL on 96.1% of scored rows since 08-04, with
              the z-scale rows being exactly the NULL ones. Any future attempt to
              partition drift by scorer identity starts there, not here.
