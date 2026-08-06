# 2026-08-06 — Persist the as-served feature matrix (unblocks rq105 shadow serving)

STATUS:   READY FOR REVIEW. Code + 27 new tests; full suite 2495 passed / 8
          skipped / 0 failed. **Default OFF** — no behaviour change until a
          destination is configured. Not yet enabled anywhere; enabling is a
          separate orchestrator-side change (see NEXT).

WHAT:     Adds `PersistFeatureSnapshotTask` as the last task of
          `BuildFeatureMatrixJob`, writing the surviving inference matrix to
          `feature_snapshot_<session_date>.json` in the schema
          `renquant_orchestrator.realtime_data_plane.FeatureSnapshot` validates.

          New module: `kernel/panel_pipeline/feature_snapshot_writer.py`.
          Enabled by `ranking.panel_scoring.feature_snapshot_dir` **or**
          `$RQ_FEATURE_SNAPSHOT_DIR`; absent in both ⇒ silent no-op.

WHY/DIR:  Measured 2026-08-06: the 94x172 inference matrix is assembled every
          run, gated, scored, and then **discarded**. It is persisted nowhere —
          `runs.alpaca.db` has 19 tables and the only score-adjacent one,
          `candidate_scores` (243,564 rows), holds outputs (raw/rank/panel score,
          mu, sigma) and no feature columns; the sole feature-shaped file on disk
          is `data/transformer_panel_labels.parquet`, which is labels.

          Two consequences:

          1. Score attribution is impossible after the fact.
          2. rq105 shadow serving requires `--feature-snapshot-json` carrying
             frozen T-1 feature values (Codex #221). **No producer has ever
             existed**, so `run_shadow_serving.sh` skipped with
             `EXIT_NOT_WIRED=4` every scheduled day and rq105 has emitted no
             intraday decision since 2026-07-14 — 23 days
             `[VERIFIED — logs/rq105/shadow_serving_2026-08-0{3,4,5}.log all read
             "SKIP not-wired: no producer exists for .../feature_snapshot_*.json";
             logs/renquant105_pilot/intraday_decisions_shadow.jsonl last record
             2026-07-14; data/rq105 holds 34 files and zero feature_snapshot_*]`.

          **The rejected alternative is the important part.** The fast fix is a
          standalone producer that REBUILDS a T-1 matrix. A rebuilt matrix is not
          necessarily the matrix the scorer saw — different data vintage, NaN
          handling, or coverage filtering all diverge — so its digest would bind
          every downstream row to a feature state that was never served. That is
          exactly the substitution #221 exists to prevent ("a bare watchlist /
          strategy-config reference is NOT a valid feature snapshot"). This
          change therefore persists **the object already in memory, after every
          gate that can still modify it**, and writes nothing when there is
          nothing genuine to write.

EVIDENCE:
artifact:      `src/renquant_pipeline/kernel/panel_pipeline/feature_snapshot_writer.py`,
               `tasks_feature_matrix.py`, `tests/test_feature_snapshot_writer.py`
prod or exp:   **prod code path** (the live order-placing scoring pipeline), but
               **inert by default**: no config in this repo sets
               `feature_snapshot_dir`, and the env var is unset, so every
               existing run takes the same `return None` and writes nothing.
existing data: live `logs/daily_104/2026-08-06.log:430` —
               `AssembleInferenceMatrixTask: X.shape=(94, 172)  ff_sub=94`
               `[VERIFIED]`. That is the object now persisted.
best-known?:   yes — this is the only source of a genuinely as-served snapshot;
               every alternative reconstructs.
scope:         `BuildFeatureMatrixJob` only. No scoring, sizing, admission, or
               order logic touched.

Cross-repo contract verified end-to-end against the **real consumer class**, not
a restatement of it `[VERIFIED — 2026-08-06, both repos on sys.path]`:

```
94x172 matrix (today's live shape), one NaN injected
  -> write_snapshot()                       463,886 B
  -> FeatureSnapshot.from_mapping()         ACCEPTED
     cutoff=2026-08-05  tickers=94  digest=sha256:ed6fc77f5da19a58...
  -> refs()                                 usable; the NaN reads back as null
  -> digest reproducible across two loads   True
```

Which twin was patched, checked rather than assumed: `grep -rn "class
AssembleInferenceMatrixTask"` returns **exactly one** definition,
`kernel/panel_pipeline/tasks_feature_matrix.py:136`, and its
`getLogger("kernel.panel_pipeline.feature_matrix")` is the logger name in the
live log line above `[VERIFIED]`. This repo does ship twin panel implementations,
so the mapping was confirmed instead of inferred from the logger name.

Tests: 27 new, all passing. Full suite **2495 passed, 8 skipped, 0 failed**.
The load-bearing ones: the payload satisfies the consumer's four validation
rules; the task runs strictly after `DriftGuardTask`; the task is advisory and
cannot abort the job; an unwritable destination, a bare context, and a missing
scorer all fail open; a missing cutoff **refuses to write** rather than
substituting today's date; NaN/Inf become explicit nulls rather than being
dropped.

## NOT ESTABLISHED

1. **That rq105 will produce useful decisions once wired.** This removes the
   structural blocker only. Whether the shadow lane's output is informative is
   untested and separate.
2. **That the snapshot is sufficient for score attribution.** It captures the
   matrix; reproducing a score also needs the scorer artifact, which is
   identified but not copied here.
3. **Any claim about the other three "dead" rq105 jobs.** orch#621's own latest
   comment refutes three of four; only `session-scheduler` remains
   unestablished, and this change does not address it.

## NEXT

Enabling is deliberately NOT in this PR. It needs `$RQ_FEATURE_SNAPSHOT_DIR` (or
the config key) pointed at `data/rq105/` for the **prod** lane — rq105 sources
from the run that placed the day's real orders. The env-var path exists so that
can be done in the reviewed launchd wrapper in `renquant-orchestrator`, without
an agent PR writing a production `strategy_config.json` — the surface Codex
blocked on strategy-104#94 the same day.

Order: merge this → point the daily-104 wrapper at `data/rq105/` → the next
daily-full emits the snapshot → `run_shadow_serving.sh` stops skipping. Only
after a real snapshot has been served should orch#621 be re-scoped or closed.

## REVERT

Delete `feature_snapshot_writer.py` and `tests/test_feature_snapshot_writer.py`,
drop `PersistFeatureSnapshotTask` from `BuildFeatureMatrixJob.tasks` and from
`__all__`, and remove the `feature_snapshot_writer` import in
`tasks_feature_matrix.py`. No other file changes; nothing else imports it.
