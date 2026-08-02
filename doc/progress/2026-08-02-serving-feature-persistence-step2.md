# Serving feature persistence — rollout step 2: the writer + sidecar land in the pipeline

STATUS: delivered — additive recorder wired at the kernel serving-transform
sites + `serving_features` sidecar staged on the run-bundle-collected payload
surface + 18 new tests + full-suite regression; PR open under review.
REVISED 2026-08-02 after codex CHANGES_REQUESTED (HIGH, reproduced): the
`run_native_inference_snapshot` facade — the exact surface
renquant-orchestrator's `native_live_inference` consumes — wrote
`snapshot.to_runtime_payload()` without finalizing a staged matrix, so the
advertised contract was not delivered on that real consumer path. Fixed +
2 regression tests below.
WHAT: implements rollout step 2 of the MERGED design
`doc/design/2026-08-02-serving-feature-persistence.md` (pipeline#250). The
kernel `ApplyScoresTask` now records the AS-SERVED feature matrix — the
IDENTICAL object the snapshot scorer consumes, post `transform_feature_frame`
— at all three snapshot consumption sites
(`kernel/panel_pipeline/job_panel_scoring.py:1351` before
`scorer.score_raw(X)` (panel_linear), `:1650` before
`scorer.score(X_aligned, ctx=ctx)` (panel_ltr_xgboost post-transform / blend
raw-union), `:1672` before `scorer.score(X, ctx=ctx)` (plain snapshot
path)). The staged copy is written to `<run_output_dir>/serving_features.parquet`
(columns: `ticker` + the exact AS-SERVED feature columns) either immediately
(when `ctx.run_output_dir` is known) or by the payload writers in
`inference.py`, whose output parent IS the run output dir — the
decision_trace precedent. The additive sidecar block
`serving_features = {path, sha256, n_rows, n_cols, feature_cutoff,
feature_builder_version, panel_read_sha256, status}` rides
`runtime_inference_payload` / `LiveContextSnapshot.to_runtime_payload` —
the surface the orchestrator's bundle collects — exactly as the wash-sale
records (#251) stage theirs; the orchestrator-side pickup into
`run_bundle.json` is the design's rollout step 3, a separate PR.
WHY/DIR: the design's measured gap — the run bundle carries 0 feature keys
against ~290 decision-trace rows (orch#678), the served matrix ceases to
exist when the run ends (orch#703), and input vintages are not
byte-reproducible after a rebuild — so what is not persisted on day T is
unrecoverable in principle. Rollout: 1 design (MERGED #250) → **2 this PR**
→ 3 orchestrator bundle pickup → 4 pin batch (operator). Nothing reaches
the live run until step 4.
EVIDENCE:
  artifact:      tests/test_serving_feature_persistence.py (16 tests);
                 src/renquant_pipeline/serving_features.py;
                 the 3 wired sites in
                 src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py
  prod or exp:   exp — additive recorder; nothing writes on the live path
                 until the pin batch (step 4). The current live primary is
                 kind hf_patchtst (`requires_history=True`,
                 `score_with_history`), which consumes NO snapshot matrix —
                 on such runs the recorder stages nothing and the block is
                 absent (pinned by the history-scorer test). The design's
                 named target (~290×~172 snapshot matrix) is the
                 panel_ltr_xgboost/blend path, which sites :1650/:1672 cover.
  existing data: design #250 (MERGED, governs); orch#678 measurement (0
                 feature keys vs 290 decision rows); orch#703 (matrix is an
                 in-memory ctx attribute, no write call anywhere); orch#647
                 (FeatureSnapshot can be read and never written)
  best-known?:   yes — first implementation of the governed design; the
                 sidecar reuses the literal `feature_builder_version` key the
                 downstream contract requires (FeatureSnapshot.from_mapping,
                 RunProvenance), valued from the artifact's existing
                 `feature_preprocess_version` stamp (= 2 on the current prod
                 artifact `[VERIFIED — read from
                 backtesting/renquant_104/artifacts/prod/panel-ltr.alpha158_fund.json,
                 2026-08-02]`) — no invented constant. `panel_read_sha256`
                 records the sha of a panel parquet WHEN one is read on the
                 serving path; the snapshot paths read none today, so the
                 honest value is None (misattributing e.g.
                 sec_fundamentals_daily.parquet would be a wrong binding).
  scope:         "this is tests/test_serving_feature_persistence.py (16
                 tests) + full pipeline suite + a manual 290-row synthetic
                 end-to-end smoke, exp path (recorder inert on the live
                 hf_patchtst primary; additive-absent everywhere else), vs
                 baseline = pristine origin/main behavior (payload
                 byte-identical when the recorder never fired — pinned by
                 the additive test)"

  Measured counts. New file: `pytest -q
  tests/test_serving_feature_persistence.py` → **16 passed** `[VERIFIED —
  2026-08-02, PR worktree]`. Full suite on this head: **2363 collected;
  2353 passed, 9 skipped, 2 failed** `[VERIFIED — make test in the PR
  worktree, 2026-08-02]`. Baseline at `origin/main` 7f8b0a5 in a
  SIBLING-located worktree (same venv, same machine): **2347 collected;
  2337 passed, 9 skipped, 2 failed**, and the 2 failing ids are the SAME
  two `test_replay_d6_conventions` pin-platform tests — pre-existing, not a
  regression `[VERIFIED — make test at 7f8b0a5, 2026-08-02]`. Collected-id
  diff main→head: **+16** all in `tests/test_serving_feature_persistence.py`,
  plus one RENAME in `tests/test_twin_pairs_one_sided_repin.py`
  (`…file_is_empty_and_well_formed` → `…file_is_well_formed_and_every_entry_is_bound`,
  re-pinned to the first real exception entry); 0 removed elsewhere
  `[VERIFIED — comm on sorted --collect-only id lists, 2026-08-02]`. Passed
  delta 2353 − 2337 = 16 `[DERIVED — from the two VERIFIED runs]`.
  Manual smoke (290 tickers × 3 features through the REAL ApplyScoresTask +
  payload writer): parquet bytes == the matrix the scorer consumed (True),
  block sha256 == recomputed file sha (True), 290/290 candidates scored,
  file 12197 bytes `[VERIFIED — scripted smoke, 2026-08-02]`.

  Twin-registry discipline: the kernel-only `ApplyScoresTask` movement is
  re-pinned (`twin_pairs.json` regenerated via `tools/twin_pairs.py --emit`)
  with a justified entry in `twin_repin_exceptions.json` — the recorder's
  sites exist only in the kernel copy; the public twin scores a
  caller-supplied dict matrix and never runs the serving transform the
  design names. `tools/twin_pairs.py` verify AND `--diff-against
  <origin/main pins>` both clean `[VERIFIED — 2026-08-02]`. The
  one-sided-repin unit tests now pass `exceptions=[]` explicitly (they were
  silently reading the committed exceptions file — the operator's-disk
  shape; the first real entry exposed it).

NEXT: (3) orchestrator consumes the staged block into `run_bundle.json`
(its additive-block pattern — `serving_bundle`/`g4_session` precedents;
`write_staged_serving_features(ctx, output_dir)` and
`serving_features_bundle_block(ctx)` are exported for exactly that) → (4)
pin batch (operator). After the first live run persists a matrix, the
Stage-3 producer design (orch#647) can be written against real bytes. AC6
gate-design rule: N/A — no capital-admission gate added, tightened, or
loosened; recorder on an existing computation (failure mode = recorded
`status: write_failed`, never a changed decision — pinned by the
decisions-unchanged test).

## Where each design clause landed

* **"Immediately after the serving transform, before scoring consumes it (the
  matrix object identical)"** — `stage_serving_features(ctx, matrix, scorer)`
  is called with the IDENTICAL object at the three consumption sites; it
  freezes a deep copy at that instant, so later in-place mutation of
  `ctx._panel_matrix` (the sentiment-gate pattern) cannot change what is
  persisted (pinned by the mutation-immunity test). When the context does
  not yet know its run output dir (today's kernel reality — the dir is
  runner-owned), the write completes in the payload writers whose
  `out.parent` is the run output dir; the byte-for-byte tests prove the
  deferred write persists exactly the consumed bytes.
* **Sidecar keys** — design-verbatim, plus `status` (`written` /
  `write_failed`) and `error` on failure, per the design's record-don't-raise
  clause.
* **"The writer NEVER raises into the decision path"** — every entry point
  is wrapped; failures stamp `status: write_failed` + error string; the
  decisions-unchanged test pins per-candidate scores byte-equal between a
  failing-writer run and a control run.
* **Absent-tolerance** — no staging ⇒ no payload key ⇒ payload byte-identical
  to pre-change (the additive idiom's standard test, same as wash-sale #251).

## Revision 2 (same day): the facade coverage hole codex reproduced

Codex CHANGES_REQUESTED on #252 with a probe on the head: a fake pipeline
staged a matrix, then `run_native_inference_snapshot(..., output_json=...)`
produced staged_present=true but snapshot/payload/parquet all FALSE — the
facade (`src/renquant_pipeline/native_inference.py`) built
`live_context_snapshot_from_live_context(ctx)` and wrote the payload
WITHOUT the finalization step the `inference.py` writers got. Real hole:
that facade is what renquant-orchestrator's `native_live_inference`
consumes.

Fix `[VERIFIED — tests below, 2026-08-02]`:

* `native_inference.py`: when `output_json` is given, the facade now calls
  `write_staged_serving_features(ctx, out.parent)` BEFORE building the
  snapshot (payload parent = run output dir, identical to the inference.py
  writers), so the snapshot AND the written payload carry the completed
  block. Record-don't-raise semantics unchanged.
* `serving_features.py`: a SUCCESSFUL write now consumes the staged copy
  (`_serving_features_staged` → None) — idempotency becomes structural (a
  second finalizer can only return the completed record, never write a
  divergent second parquet); a FAILED write keeps the staged copy so a
  later finalizer may retry.
* 2 regression tests in `tests/test_native_inference.py`: the probe's four
  false flags flipped true (snapshot block, payload block, parquet next to
  output_json byte-equal to the staged matrix, recomputed sha256 match, +
  staged-state consumed), and the no-staging control (no key, no parquet —
  byte-identical facade output).

Re-measured counts: new tests now **18** (16 + 2)
`[VERIFIED — pytest -q tests/test_serving_feature_persistence.py
tests/test_native_inference.py: 21 passed = 18 new + 3 pre-existing facade
tests, 2026-08-02]`. Full suite on the revised head: **2365 collected;
2355 passed, 9 skipped, 2 failed** — the same two pre-existing
`test_replay_d6_conventions` pin-platform failures `[VERIFIED — make test,
2026-08-02]`. Delta vs the pre-fix head 2353: +2 = exactly the two new
facade tests `[DERIVED]`. Twin pins re-emitted for the moved un-twinned
exports (`run_native_inference_snapshot`, the inference.py payload
surfaces); verify AND `--diff-against` origin/main both clean — the
ApplyScoresTask exception still binds (kernel digest unmoved this
revision) `[VERIFIED — tools/twin_pairs.py, 2026-08-02]`.
