# 2026-08-07 — Report the zero-drift floor beside every PSI; it shows the drift is REAL

STATUS:   FIXED (2026-08-07, both CHANGES_REQUESTED findings addressed). 14
          new tests; drift suite 38 passed, 1 skipped
          `[VERIFIED — python3 -m pytest tests/ -q -k drift]`; full suite 2530
          passed / 9 skipped / 2 pre-existing unrelated failures in
          `tests/test_replay_d6_conventions.py` (reproduced identically on
          the unmodified pre-fix head)
          `[VERIFIED — python3 -m pytest tests/ -q]`. **Changes no verdict** —
          `severity` still comes from `psi` alone.

WHAT:     Adds `null_psi_floor(n_baseline, n_current, bins)` and two reported
          fields on `DriftReport`: `null_floor` (median PSI under zero drift at
          THIS comparison's sizes) and `excess_over_floor` (`psi / floor`).

WHY/DIR:  The bands are the textbook PSI cut-offs and assume comparably sized
          samples. Production never had them
          `[VERIFIED — sqlite3 data/runs.alpaca.db "SELECT COUNT(*), AVG(n_baseline),
          MAX(n_current) FROM score_drift_audits"` → `1082, 1524.0, 94`; median via
          `"SELECT n_current FROM score_drift_audits ORDER BY n_current LIMIT 1
          OFFSET (SELECT COUNT(*)/2 FROM score_drift_audits)"` → `83]`:
          `n_baseline` ~1,500 against `n_current`
          **under 100 every single time** (median 83). At that shape the
          zero-drift median PSI is **~0.118**, ~7x the ~0.016 of a matched
          comparison, because `psi()` floors an empty bin at `1e-6` and one
          empty bin alone contributes ~1.15 — 4.6x the whole CRITICAL threshold.

          So `CRITICAL` (>=0.25) sits barely 2x above where a perfectly stable
          model lands, and 83% of live audits fire it. Reporting the floor makes
          the difference between "0.345, CRITICAL" and "0.345 against a 0.118
          floor" visible without moving a single threshold.

## WHAT THE FLOOR REVEALED — and how it corrects ME, twice

Re-banding all 1,082 live audits by `excess = psi / floor`
`[VERIFIED — python3 scripts/audit_score_drift_excess.py --db data/runs.alpaca.db]`:

```
excess < 1.0  (below the zero-drift floor)    35    3.2%
excess 1.0-1.5                                106    9.8%
excess 1.5-2.0                                 73    6.7%
excess 2.0-3.0                                370   34.2%
excess >= 3.0                                 498   46.0%
median 2.49x    max 94.4x
CRITICAL rows sitting below the floor:          0
```

**Not one CRITICAL has ever fired below the noise floor, and 80% of audits sit
at >=2x it. The drift is real and large.**

That refutes two framings I published earlier today, both erring toward "nothing
is wrong", which is the more dangerous direction:

1. *"A statistical artifact; the number is not trustworthy."* Wrong — a placebo
   at n=83 fires CRITICAL only 6% of the time, so sample size never explained
   the 83%.
2. *"The threshold is uncalibrated and the excess over the floor is 0.227,
   undiagnosed."* True but far too soft: the excess is a **2.49x median**, and
   the zero false-positive count says the detector has been right all along
   while being unreadable.

The field I built to make PSI legible immediately proved my own reading of it
too generous. That is the intended use.

EVIDENCE:
artifact:      `kernel/score_drift.py`, `tests/test_score_drift_noise_floor.py`
prod or exp:   **prod code path**, verdict-preserving. `severity` is still
               `severity(psi)`; the two new fields are reported and never gated
               on. Existing callers that ignore them are unaffected (both have
               defaults).
existing data: `runs.alpaca.db::score_drift_audits`, 1,082 rows.
best-known?:   yes for the floor at these shapes. **No** for what the drift IS —
               see NOT ESTABLISHED.
scope:         one function, two dataclass fields, one call site.

The estimate is seeded and cached on `(n_baseline, n_current, bins, trials,
seed)`, since the floor depends only on the SHAPE of the comparison, not on
the values, but a caller raising precision or varying the RNG must still get a
fresh estimate rather than the first call's stale one (fix below). Two readers
comparing notes at the same `trials`/`seed` therefore see the same number, and
a day of repeated audits at one shape pays for it once. `excess` is NaN —
never `inf` — when the floor is unusable, because `inf` sorts to the top of
any "worst first" list and would hijack triage.

NEXT:     Surface `null_floor` / `excess_over_floor` in the drift alert text and
          in `score_drift_audits`, so the 17 unacked incidents can be ranked by
          excess instead of by a raw PSI whose scale nobody can read. Only after
          that is there a basis for deciding whether to move a threshold — and
          on this evidence the threshold is not the problem.

## NOT ESTABLISHED

1. **What is drifting.** A 2.49x median excess says the score distribution has
   moved; it does not say which feature, regime, or artifact moved it. That
   diagnosis is separate work and this change does not attempt it.
2. **That the 17 unacked incidents are individually valid.** None was examined
   one by one; the aggregate says they are not noise.
3. **That the normal-law placebo is the right null.** The floor is estimated
   from Gaussian draws. If real score distributions are far from Gaussian the
   floor is approximate — it is a scale marker, not a p-value.

## Fix (2026-08-07, review findings addressed)

WHAT:      Finding 1 (MED) — `_NULL_FLOOR_CACHE` keyed only on
           `(n_baseline, n_current, bins)`, so a call with a different
           `trials` or `seed` after the first call at a given shape silently
           returned the first call's cached value instead of recomputing.
           Widened the key to `(n_baseline, n_current, bins, trials, seed)`
           (`src/renquant_pipeline/kernel/score_drift.py`).
           Finding 2 (MED) — the two `[VERIFIED]` tags on the 1,082-row /
           median-83 claim and the excess re-banding table cited a date and
           a table name instead of a runnable command/file (LONG #10). Reran
           both against the live `data/runs.alpaca.db` and replaced the tags
           with the exact `sqlite3` queries and a new read-only reproduction
           script, `scripts/audit_score_drift_excess.py`, that recomputes the
           excess table from `null_psi_floor` directly.
EVIDENCE:  artifact:      `tests/test_score_drift_noise_floor.py::
                          test_a_different_trials_or_seed_is_not_served_the_stale_cached_value`,
                          `tests/test_audit_score_drift_excess.py`
           prod or exp:   prod code path (cache-key fix), verdict-preserving;
                          the new script is read-only tooling, no config/pin
                          change
           existing data: reran the excess re-banding against the live DB —
                          `python3 scripts/audit_score_drift_excess.py --db
                          data/runs.alpaca.db` reproduces the PR's table
                          exactly: 35/106/73/370/498 rows per band, median
                          2.49x, max 94.4x, 0 CRITICAL rows below the floor
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db data/runs.alpaca.db]`. Drift suite 38 passed, 1
                          skipped; full suite 2530 passed / 9 skipped / 2
                          pre-existing unrelated failures (confirmed identical
                          on the unmodified pre-fix head via `git stash`).
           best-known?:   yes — closes both reviewer-identified gaps
           scope:         "cache-key widening + one new read-only script +
                          regression tests only; no config/pin/artifact
                          change, no verdict change"
NEXT:      none — addresses both CHANGES_REQUESTED findings on this PR.

## REVERT

Delete `null_psi_floor`, `_NULL_TRIALS`, `_NULL_FLOOR_CACHE`, the two
`DriftReport` fields and their computation in `score_drift_report`,
`scripts/audit_score_drift_excess.py`, and
`tests/test_score_drift_noise_floor.py` /
`tests/test_audit_score_drift_excess.py`. No other file changes.
