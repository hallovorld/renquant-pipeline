# 2026-08-07 — Report the zero-drift floor beside every PSI; it shows the drift is REAL

STATUS:   FIXED (2026-08-07, both CHANGES_REQUESTED findings addressed, then
          the P1 calibration finding fixed, then a 5th-round cache-key
          exactness finding fixed, then a 6th-round baseline-parity finding
          fixed, then a 7th round that found the 6th round's own parity
          check was still not proof — see CORRECTION below — then an 8th
          round that found the 7th round's own new provenance column was
          unreachable through the monitor's `--persist` entry point — see
          Fix (8th review round) below). Drift suite 49 passed, 1 skipped
          `[VERIFIED — python3 -m pytest tests/ -q -k drift]`; full suite
          2541 passed / 9 skipped / 2 pre-existing unrelated failures in
          `tests/test_replay_d6_conventions.py` (confirmed unrelated — that
          file is untouched by this PR; same 2 failures reproduce identically
          on the unmodified pre-fix head via `git stash`)
          `[VERIFIED — python3 -m pytest tests/ -q]`. **Changes no verdict** —
          `severity` still comes from `psi` alone.

WHAT:     Adds `null_psi_floor(baseline, n_current, bins)` and two reported
          fields on `DriftReport`: `null_floor` (median PSI under zero drift,
          conditioned on the real baseline) and `excess_over_floor`
          (`psi / floor`).

WHY/DIR:  The bands are the textbook PSI cut-offs and assume comparably sized
          samples. Production never had them
          `[VERIFIED — sqlite3 /Users/renhao/git/github/RenQuant/data/runs.alpaca.db "SELECT COUNT(*), AVG(n_baseline),
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
baseline is both still on disk AND reconstructs to exactly the size the row
was originally audited against (see the P1 and 6th-round fixes below). Of
the 1,082 logged audits, 27 pass that parity check
`[VERIFIED — python3 scripts/audit_score_drift_excess.py --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]`:

```
n_rows=1082 n_unreconstructable=1055 n_scored=27
excess < 1.0  (below the zero-drift floor)      1    3.7%
excess 1.0-1.5                                  2    7.4%
excess 1.5-2.0                                  3   11.1%
excess 2.0-3.0                                  6   22.2%
excess >= 3.0                                  15   55.6%
median 3.41x    max 107.8x
CRITICAL rows sitting below the floor:          0
```

~~**Zero CRITICAL rows sit below the correctly-conditioned floor, and the
median excess is 3.41x — the drift is real, on a smaller but honestly
scoped sample.** The sample shrank further from the P1 fix's 37 rows to 27
once the 6th-round fix (below) required the reconstructed baseline to match
the row's stored `n_baseline` exactly, rather than accepting any surviving
prefix of prior runs; the direction of the verdict is unchanged. The 1,055
unreconstructable rows are not silently dropped from the reported total;
`n_unreconstructable` is a first-class field in the script's output
specifically so a reader sees the coverage gap, not just the 27 that could
be verified.~~ **WITHDRAWN — see CORRECTION (7th review round) immediately
below.** The 27-row table's own "parity" proof turned out to be insufficient;
0 rows are currently verifiable.

## CORRECTION (2026-08-07, 7th review round) — the 27-row / 3.41x table above is WITHDRAWN as evidence

The 27-row re-banding above (and the 37-row / 4.14x table from the P1 fix
before it) rested on treating "the reconstructed baseline's SIZE matches the
row's stored `n_baseline`" as proof that `_reconstruct_baseline()` recovered
the exact run window the row was originally audited against. Reviewer P1
(7th round) showed that is not proof: `candidate_scores` prunes whole runs
over time, and when an original trailing run is pruned while an OLDER,
un-pruned run happens to hold the same score count, the count-based
reconstruction silently substitutes the wrong run into the window and still
reports parity. Reviewer's minimal counterexample: historical `run2` was
audited against baseline `[run0, run1]` (`n_baseline=80`); `run1` is later
pruned but an older `run-1` survives holding the same 40-score count — the
old code reconstructs `[run-1, run0]`, sees size 80, and reports it
verified, even though half the real baseline (`run1`) is gone.

Fix: `kernel.score_drift.DriftReport` now carries `baseline_run_ids` — the
exact `run_id`s that made up the baseline, in trailing-window order —
persisted to a new `score_drift_audits.baseline_run_ids_json` column at
audit time (`kernel/persistence.py::record_score_drift_audit`, migrated via
the existing idempotent `_COLUMN_MIGRATIONS` path so pre-existing DBs gain
the column without a manual migration). The re-banding script
(`scripts/audit_score_drift_excess.py`) now only scores a row when EVERY
`run_id` in its persisted list still has raw scores in `candidate_scores` —
an exact identity check, not an inferred one — with the old size check kept
only as a final sanity backstop. `tests/test_audit_score_drift_excess.py::
test_full_run_substitution_is_not_silently_accepted` reproduces the
reviewer's exact counterexample and asserts it is now correctly
unreconstructable.

**Consequence: every row logged before this column existed — all 1,082 rows
currently in `data/runs.alpaca.db` — carries no provenance and is
unconditionally unreconstructable**, regardless of whether its count would
have matched. Re-running the script against the live DB today (read-only;
this agent may not write to that path — verified untouched via `md5` before
and after):
```
n_rows=1082 n_unreconstructable=1082 n_scored=0
```
`[VERIFIED — python3 scripts/audit_score_drift_excess.py --db
/Users/renhao/git/github/RenQuant/data/runs.alpaca.db]`. The script also now
degrades gracefully (instead of crashing with "no such column") when run
against a DB that predates the migration, exactly this production DB's
current state
(`tests/test_audit_score_drift_excess.py::test_a_db_that_predates_the_provenance_column_does_not_crash`).

**This withdraws the 27-row (median 3.41x) and 37-row (median 4.14x)
evidentiary claims** in this section and in the "Fix (2026-08-07, P1 ...)"
and "Fix (2026-08-07, baseline parity — 6th review round)" sections below —
not because the underlying drift is now believed to be fake, but because the
specific proof method used to re-band historical rows is now known to be
unsound, and the corrected method has zero rows to re-band yet. **The
floor-calibration fix itself is unaffected and still live in
`kernel/score_drift.py`** — `null_psi_floor` still conditions correctly on
the real baseline shape, and `severity(psi)` is untouched; what's withdrawn
is only the historical-reconstruction evidence that tried to prove the
floor-adjusted excess on *already-logged* rows. Coverage now grows
forward-only: every new `run_score_drift_audit()` call persists
`baseline_run_ids_json`, so a re-banding run some weeks from now will have
real, exactly-provable rows to score. Until enough of those accrue, "is the
drift real" reverts to `severity(psi)` alone, un-recalibrated against the
floor — see NOT ESTABLISHED (updated below).

That refutes two framings I published earlier today, both erring toward "nothing
is wrong", which is the more dangerous direction:

1. *"A statistical artifact; the number is not trustworthy."* Wrong — a placebo
   at n=83 fires CRITICAL only 6% of the time, so sample size never explained
   the 83%. (This framing is about the floor calibration, which the
   CORRECTION above does not touch.)
2. ~~*"The threshold is uncalibrated and the excess over the floor is 0.227,
   undiagnosed."* True but far too soft: the excess is a **4.14x median** on
   the verifiable sample, and the zero false-positive count says the
   detector has been right all along while being unreadable.~~ **Stale even
   before the 7th round — MED-2 (7th review round) flagged this "4.14x"
   figure as inconsistent with the 6th round's own 3.41x table above it, and
   the 7th round's CORRECTION further withdraws both: 0 rows are currently
   verifiable, so no median-excess claim can be made from historical data
   right now.**

The field I built to make PSI legible immediately proved my own reading of it
too generous. That is the intended use.

EVIDENCE:
artifact:      `kernel/score_drift.py`, `tests/test_score_drift_noise_floor.py`
prod or exp:   **prod code path**, verdict-preserving. `severity` is still
               `severity(psi)`; the two new fields are reported and never gated
               on. Existing callers that ignore them are unaffected (both have
               defaults).
existing data: `runs.alpaca.db::score_drift_audits`, 1,082 rows logged; **0**
               currently reconstructable — the 27-row figure this line
               originally cited relied on a count-based parity check the
               7th review round (CORRECTION above) proved insufficient.
               `candidate_scores` pruning means historical rows cannot be
               retroactively re-proven; coverage grows forward-only from
               `baseline_run_ids_json` (see CORRECTION).
best-known?:   yes for the floor's estimation *method*, conditioned on the
               real baseline (P1 fix below) — that part of this fix is
               unaffected by the CORRECTION. **No** for what the drift IS on
               historical data — see NOT ESTABLISHED and the CORRECTION.
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
          that is there a basis for deciding whether to move a threshold — ~~and
          on this evidence the threshold is not the problem~~ **(7th-round
          CORRECTION: that call was based on the now-withdrawn 27-row sample;
          re-decide once new provenance-tagged rows accrue.)**

## NOT ESTABLISHED

1. ~~**What is drifting.** A 4.14x median excess says the score distribution
   has moved; it does not say which feature, regime, or artifact moved it.~~
   **Superseded by the 7th-round CORRECTION above: the 4.14x (and 3.41x)
   figures are withdrawn — 0 historical rows are currently verifiable, so
   there is currently no floor-adjusted excess figure at all, let alone a
   diagnosis of what moved it.** What remains established is only the
   floor-calibration *method* (`null_psi_floor` conditions on the real
   baseline); applying it to history is pending new provenance-tagged rows.
2. **That the 17 unacked incidents are individually valid.** None was examined
   one by one; the aggregate says they are not noise.
3. ~~**That the normal-law placebo is the right null.**~~ **RESOLVED by the P1
   fix below** for the estimation *method*. The floor is no longer a
   same-size Gaussian draw; it is resampled from the real baseline array, so
   it is conditioned on the baseline's actual distribution (ties included),
   not an assumed normal law. ~~Residual: only the 27 audits whose raw
   baseline is both still on disk in `candidate_scores` and reconstructs to
   exactly the row's stored `n_baseline` can be verified this way (see WHAT
   THE FLOOR REVEALED and the 6th-round fix below).~~ **Superseded by the
   7th-round CORRECTION: that "27" relied on count-based parity, which was
   not actually proof of reconstruction. Currently 0 audits are verifiable
   this way; coverage grows forward-only as `baseline_run_ids_json` accrues.**

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
           both against the live `/Users/renhao/git/github/RenQuant/data/runs.alpaca.db` and replaced the tags
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
                          /Users/renhao/git/github/RenQuant/data/runs.alpaca.db` reproduces the PR's table
                          exactly: 35/106/73/370/498 rows per band, median
                          2.49x, max 94.4x, 0 CRITICAL rows below the floor
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]`. Drift suite 38 passed, 1
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
                          /Users/renhao/git/github/RenQuant/data/runs.alpaca.db` → `n_rows=1082
                          n_unreconstructable=1045 n_scored=37`, bands
                          1/3/5/6/22, median 4.14x, max 107.8x, 0 CRITICAL
                          rows below floor
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]` **— WITHDRAWN by the
                          7th-round CORRECTION above (same count-based-parity
                          insufficiency as the 6th round's 27-row figure); 0
                          rows are currently verifiable.** Drift suite 40 passed, 1
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

## Fix (2026-08-07, cache-key exactness — 5th review round)

WHAT:      P1: `_baseline_key()` identified a baseline by a fixed 21-point
           (5%) quantile grid, independent of the requested `bins`. Two
           distinct baselines can share that coarse grid while `psi()` —
           which bins on the ACTUAL requested `bins`, not on 21 fixed points
           — reads different edges/counts from them: with ties straddling a
           quantile boundary, or simply whenever `bins > 20` reads finer
           edges the 21-point grid never captured. The old key would then
           silently serve one baseline's cached floor to a query for the
           other. Reproduced exactly on this head with a constructed pair
           sharing the 21-point grid but differing at `bins=30`: the second
           baseline's query returned the first baseline's cached floor
           (1.46) instead of its own (2.96) — confirmed by temporarily
           reinstating the old key function and rerunning the new test.
           Replaced `_baseline_key()` with a content digest
           (`hashlib.sha256` of the baseline's bytes) — exact by
           construction for any `bins` or tie pattern, at negligible cost
           next to the `trials`-loop resampling it guards
           (`src/renquant_pipeline/kernel/score_drift.py`).
EVIDENCE:  artifact:      `tests/test_score_drift_noise_floor.py::
                          test_a_baseline_that_shares_the_old_21point_grid_gets_its_own_floor`
           prod or exp:   prod code path (cache-key fix only), verdict-
                          preserving — `severity` is still `severity(psi)`;
                          no change to the floor's estimation method (that
                          was the P1 fix above), only to how it is cached.
           existing data: n/a — this is a cache-correctness fix, not a new
                          measurement; the live-DB excess numbers (median
                          4.14x, 0 CRITICAL below floor) are unaffected
                          because production baselines rarely collide on a
                          21-point grid — the defect was a latent
                          correctness bug, not one shown to have fired on
                          logged data.
           best-known?:   yes — closes the 5th-round finding; the key is now
                          an exact digest of what `psi()` actually reads
           scope:         "one function's cache-key implementation + one
                          import + regression test only; no config/pin/
                          artifact change, no verdict change. Drift suite 41
                          passed, 1 skipped
                          `[VERIFIED — python3 -m pytest tests/ -q -k drift]`;
                          full suite 2533 passed / 9 skipped / 2 pre-existing
                          unrelated failures in
                          `tests/test_replay_d6_conventions.py` (confirmed
                          identical on the unmodified pre-fix head via `git
                          stash`) `[VERIFIED — python3 -m pytest tests/ -q]`.
                          `ruff check` clean on both changed files."
NEXT:      none — closes the 5th-round finding.

## Fix (2026-08-07, baseline parity — 6th review round)

WHAT:      MED: `audit()` selected `n_current` but never `n_baseline` from
           `score_drift_audits`, and `_reconstruct_baseline()` accepted any
           surviving prefix of prior full runs as "the" baseline — even one
           shorter than what the row was actually audited against, when
           only some of its trailing runs have since been pruned from
           `candidate_scores`. Reviewer's synthetic repro: a row logged with
           `n_baseline=80` but only one of its two original trailing runs
           (40 scores) still on disk — `audit()` returned
           `n_unreconstructable=0, n_scored=1`, silently scoring the row off
           half the baseline it was logged against.
           Fixed by selecting `n_baseline` in the query and requiring
           `baseline.size == int(n_baseline)` exactly before scoring a row;
           rows that reconstruct to any other size now count toward
           `n_unreconstructable` instead of being silently scored
           (`scripts/audit_score_drift_excess.py`). Re-ran against the live
           DB — see WHAT THE FLOOR REVEALED above for the corrected 27-row
           re-banding (median 3.41x, 0 CRITICAL rows below floor, unchanged
           verdict; sample shrank from the P1 fix's 37 rows because several
           of those had a partially-pruned baseline that passed the old
           "reconstructs to something" check but not this exact-parity one).
           Two existing tests hardcoded a placeholder `n_baseline=1509`
           unrelated to their fixture's actual trailing-run count; updated
           both to the value their fixture actually reconstructs to (800 and
           40 respectively) so they exercise parity rather than accidentally
           depending on the check being absent.
EVIDENCE:  artifact:      `tests/test_audit_score_drift_excess.py::
                          test_a_row_whose_baseline_was_partially_pruned_is_not_silently_scored`
                          (this test was superseded and removed in the
                          7th-round fix below — its exact scenario is now
                          covered by `test_full_run_substitution_is_not_
                          silently_accepted`, which the 7th round rewrote to
                          the reviewer's own counterexample)
           prod or exp:   read-only tooling only (`scripts/
                          audit_score_drift_excess.py`); no change to
                          `kernel/score_drift.py`, so the prod code path and
                          `severity` computation are untouched — this fix is
                          entirely to historical re-banding accuracy
           existing data: `python3 scripts/audit_score_drift_excess.py --db
                          /Users/renhao/git/github/RenQuant/data/runs.alpaca.db` → `n_rows=1082
                          n_unreconstructable=1055 n_scored=27`, bands
                          1/2/3/6/15, median 3.41x, max 107.8x, 0 CRITICAL
                          rows below floor
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]` **— WITHDRAWN by the
                          7th-round CORRECTION above: this "27" relied on a
                          count-based parity check later shown insufficient
                          proof of reconstruction; 0 rows are currently
                          verifiable.** Drift suite 42 passed, 1
                          skipped `[VERIFIED — python3 -m pytest tests/ -q -k
                          drift]`; full suite 2534 passed / 9 skipped / 2
                          pre-existing unrelated failures in
                          `tests/test_replay_d6_conventions.py` (confirmed
                          identical on the unmodified pre-fix head via `git
                          stash`) `[VERIFIED — python3 -m pytest tests/ -q]`.
                          `ruff check` clean on both changed files.
           best-known?:   at the time — closes the 6th-round finding as
                          understood then; the script only scored a row when
                          the reconstruction was PARITY-provably the same
                          baseline, not merely "something reconstructed".
                          **No longer best-known — see the 7th-round
                          CORRECTION: parity was itself insufficient proof.**
           scope:         "one script's query + reconstruction-acceptance
                          check, two test fixtures corrected, one new
                          regression test — no config/pin/artifact change, no
                          change to the prod `null_psi_floor`/`score_drift_report`
                          path. The historical evidentiary sample shrank
                          further (37 -> 27 rows); median excess moved
                          4.14x -> 3.41x; the direction of the verdict (drift
                          is real, zero false positives below floor) is
                          unchanged."
NEXT:      none — closes the 6th-round finding.

## Fix (2026-08-07, baseline provenance + doc reconciliation — 7th review round)

WHAT:      P1 — the 6th round's own "parity" check (reconstructed baseline
           size == stored `n_baseline`) is not proof the reconstruction is
           the same run window, because `candidate_scores` prunes whole
           runs and a pruned trailing run can be silently substituted by an
           older surviving run with the same score count. Full
           counterexample and CORRECTION are recorded in the "WHAT THE
           FLOOR REVEALED" section above; not repeated here.
           Fixed by persisting the actual baseline provenance instead of
           inferring it: `kernel.score_drift.DriftReport` gained
           `baseline_run_ids: tuple[str, ...]`, threaded through
           `score_drift_report()` and populated by
           `load_score_drift_from_db()` from the exact trailing-window ids
           it already computes. `kernel/persistence.py` gained a
           `score_drift_audits.baseline_run_ids_json` column (schema +
           `_COLUMN_MIGRATIONS` entry, so pre-existing DBs migrate via the
           existing idempotent path) and `record_score_drift_audit()` now
           writes it (JSON list, NULL when absent). `scripts/
           score_drift_monitor.py::_persist_audit()` — a second write path
           that creates the table with its own inline DDL rather than going
           through `ensure_schema()` — got the same column in its DDL plus
           an explicit `PRAGMA table_info` + `ALTER TABLE` fallback for a DB
           it created before this column existed, so `--persist` does not
           crash against an older monitor-created DB.
           `scripts/audit_score_drift_excess.py::_reconstruct_baseline()`
           now takes the persisted `baseline_run_ids` list and requires
           every one of those exact ids to still have raw scores in
           `candidate_scores` — an identity check, not an inferred
           count-match — keeping the old size check only as a final sanity
           backstop. It also degrades gracefully (checks `PRAGMA
           table_info` first) instead of crashing with "no such column"
           when run against a DB that predates the migration — exactly this
           production DB's current state, since this agent cannot write to
           it to trigger the migration itself (production paths are
           read-only).
           MED 1 — every command/doc reference to `data/runs.alpaca.db`
           (a path that does not exist in this repo checkout) corrected to
           the actual supported location,
           `/Users/renhao/git/github/RenQuant/data/runs.alpaca.db`, in this
           doc and the script's `Usage:` docstring.
           MED 2 — the two stale "4.14x" mentions (this doc's "WHAT THE
           FLOOR REVEALED" prose and "NOT ESTABLISHED" item 1) that
           contradicted the 6th round's own 3.41x table are struck through
           and reconciled, per the no-silent-overwrite rule (LONG #10) —
           see the CORRECTION section above, which also documents that both
           figures are now further withdrawn, not merely reconciled to each
           other.
EVIDENCE:  artifact:      `tests/test_score_drift.py::TestDbLoader::
                          test_baseline_run_ids_are_the_trailing_window`,
                          `tests/test_score_drift_persist.py::TestRecord::
                          test_records_baseline_run_ids`,
                          `tests/test_score_drift_persist.py::TestRecord::
                          test_no_baseline_run_ids_stored_as_null`,
                          `tests/test_audit_score_drift_excess.py::
                          test_full_run_substitution_is_not_silently_accepted`
                          (reviewer's exact P1 counterexample),
                          `tests/test_audit_score_drift_excess.py::
                          test_legacy_rows_without_baseline_provenance_stay_unreconstructable`,
                          `tests/test_audit_score_drift_excess.py::
                          test_a_db_that_predates_the_provenance_column_does_not_crash`
           prod or exp:   prod code path — `kernel/score_drift.py` (new
                          dataclass field, additive, default `()`) and
                          `kernel/persistence.py` (new column, additive
                          migration) are both live write paths; `severity`
                          computation is untouched. `scripts/
                          audit_score_drift_excess.py` and `scripts/
                          score_drift_monitor.py` are read-only /
                          append-only tooling — no decision changes.
           existing data: ran the corrected script against the live
                          production DB, read-only
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]`
                          → `n_rows=1082 n_unreconstructable=1082 n_scored=0`
                          (every row predates the new column — expected,
                          since this agent did not and must not write to
                          that production path to trigger the migration).
                          Confirmed the DB was not mutated via `md5` before
                          and after the run.
           best-known?:   yes — closes the 7th-round P1 finding by replacing
                          inferred reconstruction with persisted exact
                          identity; **explicitly NOT best-known for "is the
                          historical drift real"** — that specific claim is
                          withdrawn pending new provenance-tagged rows (see
                          CORRECTION above)
           scope:         "one dataclass field + its two producers, one new
                          column + migration + writer update in two write
                          paths, one script's reconstruction check + a
                          graceful-degradation guard, regression tests, and
                          this doc's reconciliation. No config/pin/artifact
                          change; no write to any production data path.
                          Drift suite 47 passed, 1 skipped
                          `[VERIFIED — python3 -m pytest tests/ -q -k drift]`;
                          full suite 2539 passed / 9 skipped / 2
                          pre-existing unrelated failures in
                          `tests/test_replay_d6_conventions.py` (confirmed
                          identical on the unmodified pre-fix head via `git
                          stash`) `[VERIFIED — python3 -m pytest tests/ -q]`.
                          `ruff check` clean on all changed files."
NEXT:      none for this finding. The historical evidentiary gap it opens
           (0 currently-verifiable rows) closes itself passively as
           production keeps running — every future `run_score_drift_audit()`
           call persists `baseline_run_ids_json`, so a re-banding run some
           weeks out will have real rows to score. No action item beyond
           that; re-running the script periodically is enough to observe
           coverage grow.

## Fix (2026-08-07, forward-coverage entry point — 8th review round)

WHAT:      P1 — the 7th round's "coverage grows forward-only" claim is dead
           through `scripts/score_drift_monitor.py`'s `--persist` entry
           point specifically (the standalone monitor CLI, as opposed to
           `kernel.score_audit.run_score_drift_audit`, which already took an
           explicit `run_id` and was never affected). `_persist_audit()`
           called `record_score_drift_audit(conn, run_id=None, ...)`
           unconditionally, and `scripts/audit_score_drift_excess.py::audit()`
           skips every row with `run_id is None` before it ever looks at
           `baseline_run_ids_json`. So a fresh `--persist` audit got the new
           provenance column written correctly, then was permanently
           excluded from scoring by the `run_id` check regardless — the new
           column and the read path never actually connected for this entry
           point.
           Fixed by propagating the real run_id instead of removing the
           `audit()` check (removing it would let a row with no run
           identity at all masquerade as reconstructable, which is the same
           class of bug the earlier rounds fixed for the baseline side).
           `kernel.score_drift.DriftReport` gained a `run_id: str | None`
           field — the CURRENT run's id (`latest` in
           `load_score_drift_from_db`, NOT a `baseline_run_ids` member) —
           threaded through `score_drift_report()`'s new `run_id=` keyword
           and populated by `load_score_drift_from_db()`.
           `scripts/score_drift_monitor.py::_persist_audit()` now passes
           `run_id=report.run_id` instead of the hardcoded `None`.
           Added an end-to-end regression,
           `tests/test_score_drift_monitor.py::TestPersist::
           test_persisted_row_is_scored_by_the_read_only_audit`, that runs
           `monitor(..., persist=True)` then the read-only `audit()` and
           asserts the freshly persisted row is `n_scored=1`,
           `n_unreconstructable=0` — the exact scenario the reviewer asked
           for, since a unit test on `DriftReport.run_id` alone would not
           have caught the entry-point mismatch (the field existing does not
           prove the CLI wires it through). Also added
           `tests/test_score_drift.py::TestDbLoader::
           test_run_id_is_the_current_run_not_a_baseline_member`.
EVIDENCE:  artifact:      `tests/test_score_drift_monitor.py::TestPersist::
                          test_persisted_row_is_scored_by_the_read_only_audit`,
                          `tests/test_score_drift.py::TestDbLoader::
                          test_run_id_is_the_current_run_not_a_baseline_member`
           prod or exp:   prod code path — `kernel/score_drift.py` (new
                          dataclass field, additive, default `None`) is a
                          live path; `scripts/score_drift_monitor.py` is the
                          CLI's only write-enabling change (one `None` ->
                          `report.run_id`). `severity` computation and
                          `kernel.score_audit.run_score_drift_audit` (the
                          other, already-correct write path) are untouched.
           existing data: n/a — this is a coverage-path fix, not a new
                          historical measurement. Re-ran the read-only audit
                          against the live production DB
                          `[VERIFIED — python3 scripts/audit_score_drift_excess.py
                          --db /Users/renhao/git/github/RenQuant/data/runs.alpaca.db]`
                          → `n_rows=1082 n_unreconstructable=1082 n_scored=0`,
                          unchanged from the 7th round (expected: this agent
                          did not and must not write a `--persist` audit to
                          that production path; the fix only unblocks
                          scoring for rows written going forward once a
                          scheduled `--persist` run accrues them).
           best-known?:   yes — closes the 8th-round P1 finding; the
                          end-to-end test proves the specific path the
                          reviewer flagged (monitor persist -> audit score)
                          rather than only the unit-level field plumbing.
           scope:         "one new dataclass field + one keyword threaded
                          through two functions, one `None` -> `report.run_id`
                          fix in the monitor CLI, two regression tests
                          (one end-to-end). No config/pin/artifact change; no
                          write to any production data path. Drift suite 49
                          passed, 1 skipped
                          `[VERIFIED — python3 -m pytest tests/ -q -k drift]`;
                          full suite 2541 passed / 9 skipped / 2 pre-existing
                          unrelated failures in
                          `tests/test_replay_d6_conventions.py` (confirmed
                          identical on the unmodified pre-fix head via `git
                          stash`) `[VERIFIED — python3 -m pytest tests/ -q]`.
                          `ruff check` clean on all changed files."
NEXT:      none for this finding. The forward-coverage claim now holds for
           BOTH write paths (`run_score_drift_audit` and the monitor CLI's
           `--persist`); whether either is actually invoked on a schedule in
           production is outside this PR's scope — this fix only removes the
           entry-point-specific dead path the reviewer found.

## REVERT

Delete `null_psi_floor`, `_baseline_key`, `_NULL_TRIALS`, `_NULL_FLOOR_CACHE`,
the two `DriftReport` fields and their computation in `score_drift_report`,
`scripts/audit_score_drift_excess.py`, and
`tests/test_score_drift_noise_floor.py` /
`tests/test_audit_score_drift_excess.py`. Also delete `DriftReport
.baseline_run_ids`, its plumbing through `score_drift_report()` /
`load_score_drift_from_db()`, the `score_drift_audits.baseline_run_ids_json`
column + its `_COLUMN_MIGRATIONS` entry + `record_score_drift_audit()`'s
write of it, and the matching DDL/migration guard in
`scripts/score_drift_monitor.py::_persist_audit()`. Also delete `DriftReport
.run_id`, the `run_id=` keyword on `score_drift_report()`, its population in
`load_score_drift_from_db()`, and revert
`scripts/score_drift_monitor.py::_persist_audit()`'s
`record_score_drift_audit(conn, run_id=report.run_id, ...)` back to
`run_id=None`. No other file changes.
