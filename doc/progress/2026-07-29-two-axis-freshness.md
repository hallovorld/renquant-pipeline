# Progress: two-axis freshness for the shadow health record (GOAL-6 decision A)

STATUS:   delivered (producer change + 8 tests; full suite 2111 passed, 0 regressions).
          Consumer-side threshold plumbing unchanged — the sentinel reads the record's
          own verdict.

WHAT:     `finalize_shadow_health` replaces a single 28-calendar-day check on
          `effective_train_cutoff_date` with two axes: (1) `trained_date` age <= 28d,
          (2) cutoff lag <= structural floor + slack, where the floor is derived from
          the artifact's OWN `lookahead_days` converted trading->calendar. New record
          fields: `trained_age_days`, `cutoff_lag_floor_days`, `cutoff_lag_bound_days`.

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
          Suite: 8 new tests, 2111 passed / 8 skipped overall.

NEXT:     The umbrella fork mirrors this in the same batch (that divergence class has
          bitten twice already). Producers must stamp `trained_date` and
          `lookahead_days` onto shadow artifacts; where they do not yet, the
          fail-closed path keeps the old behaviour and names itself.
