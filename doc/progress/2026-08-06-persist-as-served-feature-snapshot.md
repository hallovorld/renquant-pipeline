# 2026-08-06 — Persist the as-served feature matrix (unblocks rq105 shadow serving)

STATUS:   READY FOR REVIEW. Code + 32 tests (27 original + 5 added in this
          fix pass); focused suite (writer + panel-scoring consumers) 100
          passed / 1 skipped / 0 failed. **Default OFF** — no behaviour
          change until a destination is configured. Not yet enabled
          anywhere; enabling is a separate orchestrator-side change (see
          NEXT).

          **Scope narrowed in this fix pass (codex HIGH, review round 3):**
          `persist_from_context` now REFUSES to write for scorer kinds where
          `ctx._panel_matrix` is not the surface actually scored —
          `panel_linear` / `panel_ltr_xgboost` / `blend` (rebuild alpha158
          from raw OHLCV afterward) and any `requires_history` scorer
          (PatchTST / **hf_patchtst — the current production primary**,
          bypasses this matrix via `score_with_history()`). Confirmed
          `panel_lgbm` and other non-rebuild, non-history kinds are
          unaffected and still write. **Consequence: as written, this task
          currently writes NOTHING for the live production lane** (hf_patchtst)
          **or the XGBoost shadow/rollback lane** (panel_ltr_xgboost) — the
          two scorer kinds CLAUDE.md §2 names as active. It still closes the
          structural gap safely (no mislabelled snapshot can reach rq105) but
          does NOT yet unblock rq105 shadow serving off the prod lane as
          originally scoped; that needs a follow-up hooking the actual
          per-branch served surface inside `ApplyScoresTask` (`panel_history`
          for history scorers, `X_aligned` for alpha158/XGB — the latter
          already staged by `stage_serving_features()` in `serving_features.py`,
          merged separately in #268).

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
               `[VERIFIED]`. **Correction (this fix pass):** that 172-col
               shape matches hf_patchtst's `feature_cols`, but hf_patchtst is
               `requires_history=True` — the fix added here makes the writer
               refuse to persist this exact matrix for that scorer, since
               `score_with_history()` never consumes it. It is NOT the object
               persisted for the current production lane.
best-known?:   yes for scorer kinds that consume `ctx._panel_matrix` as-is
               (e.g. `panel_lgbm`); NOT yet for `hf_patchtst` (prod) or
               `panel_ltr_xgboost`/`panel_linear`/`blend` (shadow/rollback) —
               those refuse rather than reconstruct, per the scope-narrowing
               above.
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

1. **That rq105 will produce useful decisions once wired.** This removes part
   of the structural blocker only — and, after this fix pass, not yet the
   part that matters for the prod lane (see the scope-narrowing note in
   STATUS above; a follow-up PR is still required before rq105 can source
   from hf_patchtst). Whether the shadow lane's output is informative once
   that follow-up lands is untested and separate.
2. **That the snapshot is sufficient for score attribution.** It captures the
   matrix; reproducing a score also needs the scorer artifact, which is
   identified but not copied here.
3. **Any claim about the other three "dead" rq105 jobs.** orch#621's own latest
   comment refutes three of four; only `session-scheduler` remains
   unestablished, and this change does not address it.

NEXT:     Enabling is deliberately NOT in this PR. **Superseded by the
          scope-narrowing above:** pointing `$RQ_FEATURE_SNAPSHOT_DIR` at
          `data/rq105/` for the **prod** lane alone no longer unblocks
          rq105 — the prod scorer (hf_patchtst) now refuses to write. Before
          that env var is wired anywhere, a follow-up PR must hook the
          persist call to the actual per-branch served surface inside
          `ApplyScoresTask` (`panel_history` cross-section for history
          scorers; `X_aligned` for alpha158/XGB, reusing the pattern
          `stage_serving_features()` already establishes in
          `serving_features.py`, #268). Only once that follow-up lands does
          "point the wrapper at `data/rq105/`" apply.

Order: merge this (safety-narrowed, writes nothing on the two active scorer
kinds today) → follow-up PR hooks the real per-branch surface → point the
daily-104 wrapper at `data/rq105/` → the next daily-full emits the snapshot →
`run_shadow_serving.sh` stops skipping. Only after a real snapshot has been
served should orch#621 be re-scoped or closed.

## REVERT

Delete `feature_snapshot_writer.py` and `tests/test_feature_snapshot_writer.py`,
drop `PersistFeatureSnapshotTask` from `BuildFeatureMatrixJob.tasks` and from
`__all__`, and remove the `feature_snapshot_writer` import in
`tasks_feature_matrix.py`. No other file changes; nothing else imports it.
