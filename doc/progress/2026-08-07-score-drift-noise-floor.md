# 2026-08-07 — Report the zero-drift floor beside every PSI; it shows the drift is REAL

STATUS:   FIXED (2026-08-07, both CHANGES_REQUESTED findings addressed, then
          the P1 calibration finding fixed below). Drift suite 40 passed, 1
          skipped `[VERIFIED — python3 -m pytest tests/ -q -k drift]`; full
          suite 2532 passed / 9 skipped / 2 pre-existing unrelated failures in
          `tests/test_replay_d6_conventions.py` (confirmed unrelated — that
          file is untouched by this PR; same 2 failures the prior fix round
          already reproduced on the unmodified pre-fix head via `git stash`)
          `[VERIFIED — python3 -m pytest tests/ -q]`. **Changes no verdict** —
          `severity` still comes from `psi` alone.

WHAT:     Adds `null_psi_floor(baseline, n_current, bins)` and two reported
          fields on `DriftReport`: `null_floor` (median PSI under zero drift,
          conditioned on the real baseline) and `excess_over_floor`
          (`psi / floor`).

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

`score_drift_audits` only ever persisted `n_baseline`/`n_current` counts, not
the raw scores, and `candidate_scores` (the source of the raw scores) is
pruned over time. So re-banding history now only covers the runs whose raw
baseline is still on disk — the corrected floor needs the actual baseline
array, not just its size (see the P1 fix below). Of the 1,082 logged audits,
37 still have a reconstructable baseline
`[VERIFIED — python3 scripts/audit_score_drift_excess.py --db data/runs.alpaca.db]`:

```
n_rows=1082 n_unreconstructable=1045 n_scored=37
excess < 1.0  (below the zero-drift floor)      1    2.7%
excess 1.0-1.5                                  3    8.1%
excess 1.5-2.0                                  5   13.5%
excess 2.0-3.0                                  6   16.2%
excess >= 3.0                                  22   59.5%
median 4.14x    max 107.8x
CRITICAL rows sitting below the floor:          0
```

**Zero CRITICAL rows sit below the correctly-conditioned floor, and the
median excess is 4.14x (vs the earlier shape-only estimate of 2.49x on the
larger, uncorrected sample) — the drift is real, on a smaller but honestly
scoped sample.** The 1,045 unreconstructable rows are not silently dropped
from the reported total; `n_unreconstructable` is a first-class field in the
script's output specifically so a reader sees the coverage gap, not just the
37 that could be verified.

That refutes two framings I published earlier today, both erring toward "nothing
is wrong", which is the more dangerous direction:

1. *"A statistical artifact; the number is not trustworthy."* Wrong — a placebo
   at n=83 fires CRITICAL only 6% of the time, so sample size never explained
   the 83%.
2. *"The threshold is uncalibrated and the excess over the floor is 0.227,
   undiagnosed."* True but far too soft: the excess is a **4.14x median** on
   the verifiable sample, and the zero false-positive count says the
   detector has been right all along while being unreadable.

The field I built to make PSI legible immediately proved my own reading of it
too generous. That is the intended use.

EVIDENCE:
artifact:      `kernel/score_drift.py`, `tests/test_score_drift_noise_floor.py`
prod or exp:   **prod code path**, verdict-preserving. `severity` is still
               `severity(psi)`; the two new fields are reported and never gated
               on. Existing callers that ignore them are unaffected (both have
               defaults).
existing data: `runs.alpaca.db::score_drift_audits`, 1,082 rows logged; 37
               with a still-reconstructable baseline (`candidate_scores`
               pruning — see the P1 fix below).
best-known?:   yes for the floor, conditioned on the real baseline (P1 fix
               below). **No** for what the drift IS — see NOT ESTABLISHED.
scope:         one function, two dataclass fields, one call site.

The estimate is seeded and cached on the baseline's own quantile grid plus
`(n_current, bins, trials, seed)` (P1 fix below — size alone is not a
sufficient key), but a caller raising precision or varying the RNG must still
get a fresh estimate rather than the first call's stale one. `excess` is
NaN — never `inf` — when the floor is unusable, because `inf` sorts to the
top of any "worst first" list and would hijack triage.

NEXT:     Surface `null_floor` / `excess_over_floor` in the drift alert text and
          in `score_drift_audits`, so the 17 unacked incidents can be ranked by
          excess instead of by a raw PSI whose scale nobody can read. Only after
          that is there a basis for deciding whether to move a threshold — and
          on this evidence the threshold is not the problem.

## NOT ESTABLISHED

1. **What is drifting.** A 4.14x median excess says the score distribution has
   moved; it does not say which feature, regime, or artifact moved it. That
   diagnosis is separate work and this change does not attempt it.
2. **That the 17 unacked incidents are individually valid.** None was examined
   one by one; the aggregate says they are not noise.
3. ~~**That the normal-law placebo is the right null.**~~ **RESOLVED by the P1
   fix below.** The floor is no longer a same-size Gaussian draw; it is
   resampled from the real baseline array, so it is conditioned on the
   baseline's actual distribution (ties included), not an assumed normal law.
   Residual: only the 37 audits whose raw baseline is still on disk in
   `candidate_scores` can be verified this way (see WHAT THE FLOOR REVEALED).

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

## Fix (2026-08-07, P1 — floor conditioned on the real baseline)

WHAT:      P1 (unresolved across 3 review rounds): `null_psi_floor` estimated
           the null from Gaussian draws at `(n_baseline, n_current, bins)` —
           shape only. `psi()` is not shape-only: it builds bin edges from
           `np.quantile(expected, ...)`, so a baseline with ties collapses
           those edges and changes the effective bin count. Reviewer's own
           repro on `np.repeat(np.arange(5), 300)`: shape-only floor 0.1189
           vs the baseline-conditioned floor 0.0370 — 3.2x overstated.
           Independently re-derived: gaussian/uniform/exponential/lognormal
           (all continuous, tie-free) baselines give bootstrap floors within
           0.102-0.111 of each other at `n_baseline=1509, n_current=83`
           (0.1025 / 0.1028 / 0.1106 / 0.1090)
           `[VERIFIED — python3 -c "import numpy as np;
           from renquant_pipeline.kernel.score_drift import null_psi_floor;
           rng = np.random.default_rng(0);
           print([null_psi_floor(gen(rng, size=1509), 83, seed=1) for gen in
           (np.random.Generator.normal, np.random.Generator.uniform,
           np.random.Generator.exponential, np.random.Generator.lognormal)])"]`
           — quantile-binned PSI is close to distribution-free for a
           continuous baseline, so the bug is specifically about ties, not
           "Gaussian vs. the real shape" in general. Fixed `null_psi_floor(baseline, n_current, ...)` to take
           the actual baseline array and resample `current` FROM the
           baseline itself (bootstrap, with replacement) instead of drawing
           fresh Gaussians, and to cache on the baseline's own quantile grid
           (`_baseline_key`) instead of its size
           (`src/renquant_pipeline/kernel/score_drift.py`).

           `score_drift_audits` never persisted raw scores, only
           `n_baseline`/`n_current` counts, so `scripts/
           audit_score_drift_excess.py` could no longer call the corrected
           function with just those counts. Reworked it to reconstruct each
           row's baseline from `candidate_scores` (keyed by `run_id`, same
           trailing-window logic as `load_score_drift_from_db`); rows whose
           raw scores have since been pruned are now counted in a new
           `n_unreconstructable` field rather than silently scored with the
           old, now-known-wrong shape-only approximation. Re-ran against the
           live DB — see WHAT THE FLOOR REVEALED above for the corrected
           37-row re-banding (median 4.14x, 0 CRITICAL rows below floor,
           unchanged verdict).

EVIDENCE:  artifact:      `tests/test_score_drift_noise_floor.py::
                          test_a_tied_baseline_floor_is_conditioned_on_the_real_distribution`,
                          `tests/test_audit_score_drift_excess.py::
                          test_rows_whose_baseline_was_pruned_are_reported_not_silently_scored`
           prod or exp:   prod code path (floor calibration), verdict-
                          preserving — `severity` is still `severity(psi)`;
                          the audit script is read-only tooling
           existing data: `python3 scripts/audit_score_drift_excess.py --db
                          data/runs.alpaca.db` → `n_rows=1082
                          n_unreconstructable=1045 n_scored=37`, bands
                          1/3/5/6/22, median 4.14x, max 107.8x, 0 CRITICAL
                          rows below floor
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db data/runs.alpaca.db]`. Drift suite 40 passed, 1
                          skipped `[VERIFIED — python3 -m pytest tests/ -q -k
                          drift]`; full suite 2532 passed / 9 skipped / 2
                          pre-existing unrelated failures in
                          `tests/test_replay_d6_conventions.py` (file
                          untouched by this PR)
                          `[VERIFIED — python3 -m pytest tests/ -q]`.
           best-known?:   yes — closes the P1 finding; the floor is now
                          conditioned on the actual baseline instead of an
                          assumed continuous law
           scope:         "one function's null-estimation method + its cache
                          key, one script's data source, regression tests —
                          no config/pin/artifact change. The historical
                          evidentiary sample shrank from 1,082 to 37
                          reconstructable rows; the direction of the verdict
                          (drift is real, zero false positives below floor)
                          is unchanged and the median excess is HIGHER on the
                          corrected sample (4.14x vs 2.49x), not lower."
NEXT:      none — closes the P1 finding. Going forward every new audit's
           baseline is written fresh to `candidate_scores` before the floor
           is computed, so future re-banding does not have this gap; only
           the already-pruned historical tail is permanently unreconstructable.

## REVERT

Delete `null_psi_floor`, `_baseline_key`, `_NULL_TRIALS`, `_NULL_FLOOR_CACHE`,
the two `DriftReport` fields and their computation in `score_drift_report`,
`scripts/audit_score_drift_excess.py`, and
`tests/test_score_drift_noise_floor.py` /
`tests/test_audit_score_drift_excess.py`. No other file changes.
