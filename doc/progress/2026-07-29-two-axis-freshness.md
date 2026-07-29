# Progress: two-axis freshness for the shadow health record (GOAL-6 decision A)

STATUS:   delivered. Initial commit (`b69f209`) shipped only the finalizer logic
          (`shadow_health.py` + 8 unit tests) — codex review (BLOCKER) caught that
          the producer (`ApplyShadowScoringTask` in `shadow_scoring.py`) never
          populated the two new fields the finalizer reads, so the two-axis rule
          was dead code on the real path. Follow-up commit wires
          `trained_date` / `lookahead_days` from `scorer.metadata` onto the health
          record in the producer and adds an integration test exercising the real
          `ApplyShadowScoringTask.run` path (not just the finalizer in isolation).
          Consumer-side threshold plumbing unchanged — the sentinel reads the record's
          own verdict.

WHAT:     `finalize_shadow_health` replaces a single 28-calendar-day check on
          `effective_train_cutoff_date` with two axes: (1) `trained_date` age <= 28d,
          (2) cutoff lag <= structural floor + slack, where the floor is derived from
          the artifact's OWN `lookahead_days` converted trading->calendar. New record
          fields: `trained_age_days`, `cutoff_lag_floor_days`, `cutoff_lag_bound_days`.
          `ApplyShadowScoringTask` now stamps `trained_date` / `lookahead_days` from
          `scorer.metadata` onto the health record alongside the pre-existing
          `effective_train_cutoff_date` / `config_fingerprint` copy, so the two-axis
          logic actually activates for a real shadow run.

WHY/DIR:  Operator decision 2026-07-29 (option A of orch#588). The single-axis rule was
          UNSATISFIABLE for a fwd60 recipe: the last training label needs its forward
          window closed, so the cutoff can never be nearer than the horizon, and a
          model retrained this morning flagged stale on arrival. The same rule shape
          sat behind months of silently refused weekly promotions.

EVIDENCE: the two axes fail for DIFFERENT causes, both of which this project has
          actually experienced, and one number cannot watch both: axis 1 catches a
          retrain that stopped (the per-ticker tournament frozen since April); axis 2
          catches inputs that stopped advancing while retrains kept succeeding (the
          fund-freshness serving-axis clip). Bound arithmetic `[VERIFIED - direct
          call]`: 60 trading days -> 84 calendar days floor, +28d slack = 112d bound,
          so today's clf lane at 91d passes while the 622d legacy lane still flags -
          both pinned by tests. A fwd20 recipe gets a tighter 28d floor / 56d bound
          from the same code, because the horizon is read per artifact and never
          hardcoded. Absent or absurd (>252td) declared horizons FAIL CLOSED to the
          old single-axis rule with an explicit `no_declared_lookahead_single_axis`
          reason, so a strict judgement never looks like an ordinary stale flag.
          Suite (original commit `b69f209`): 8 new tests in `test_shadow_health.py`,
          full suite 2111 passed / 8 skipped `[ASSUMED - carried from the original
          PR body, not independently re-run in full for this fix]`. Follow-up fix
          adds a 9th test, `test_run_wires_two_axis_fields_fwd60_not_stale_on_arrival`
          in `tests/test_shadow_scorer_health_record.py`; `[VERIFIED - pytest -q
          tests/test_shadow_scorer_health_record.py tests/test_shadow_health.py]`
          41 passed, 0 failed.

          artifact:      `src/renquant_pipeline/kernel/panel_pipeline/shadow_health.py`
                         (finalizer logic, `finalize_shadow_health` /
                         `_freshness_reasons`) + `src/renquant_pipeline/kernel/
                         panel_pipeline/shadow_scoring.py` (`ApplyShadowScoringTask`,
                         the producer that stamps the health record) +
                         `tests/test_shadow_health.py` +
                         `tests/test_shadow_scorer_health_record.py`.

          prod or exp:   prod. `ApplyShadowScoringTask` is the real production task
                         that emits `logs/shadow_scorer_health.jsonl`, consumed by
                         the shadow-artifact CI gate (orchestrator PR #525) and the
                         shadow-health sentinel (orchestrator PR #566). It scores
                         SHADOW (comparison-only, readonly) models, not the primary
                         decision path, so a wrong verdict here degrades observability
                         of the shadow feed, not live trading decisions directly.

          existing data: `[VERIFIED - git diff d1aff06..b69f209]` the original commit
                         on this branch touched ONLY `shadow_health.py` (the
                         finalizer) + its own unit tests
                         (`tests/test_shadow_health.py`) - it never touched
                         `shadow_scoring.py`. codex review confirmed by simulating a
                         producer-shaped record (cutoff present, no `trained_date` /
                         `lookahead_days`) that `finalize_shadow_health` still
                         returned `actionable=False` with
                         `['stale_92d_limit_28d', 'no_declared_lookahead_single_axis']`
                         on PR head `b69f209` - i.e. the two-axis logic was dead code
                         on the real path because the producer never populated the two
                         new fields it reads. Confirmed the same by inspection: before
                         this fix, `ApplyShadowScoringTask` (line ~453-457, pre-fix)
                         only copied `effective_train_cutoff_date` and
                         `config_fingerprint` from `scorer.metadata` onto `health`.

          best-known?:   this fix (wiring `trained_date` + `lookahead_days` straight
                         from `scorer.metadata` onto the health record, mirroring the
                         existing two-field copy pattern already in
                         `ApplyShadowScoringTask`) is the direct, minimal completion
                         of the two-axis design already reviewed and merged in
                         `shadow_health.py` - not a stopgap. It is contingent on
                         individual scorer artifacts actually stamping
                         `trained_date` / `lookahead_days` in their own metadata;
                         where an artifact does not (yet), the fail-closed
                         `no_declared_lookahead_single_axis` path (already covered by
                         `tests/test_shadow_health.py`) keeps the old, strict
                         behaviour and names itself, per the NEXT note below.

          scope:         this PR is `shadow_scoring.py` +
                         `test_shadow_scorer_health_record.py`, prod, and fixes the
                         BLOCKER that the two-axis freshness fields were computed by
                         the finalizer but never reached it from the real producer
                         path, vs the prior behaviour where a fwd60 shadow model
                         still flagged `stale_Nd_limit_28d` on arrival despite the
                         two-axis logic having landed in `shadow_health.py`.

NEXT:     The umbrella fork mirrors this in the same batch (that divergence class has
          bitten twice already). Producers must stamp `trained_date` and
          `lookahead_days` onto shadow artifacts; where they do not yet, the
          fail-closed path keeps the old behaviour and names itself.
