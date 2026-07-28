# 2026-07-27 — Composite BLEND scorer kind (certified z(prod)+z(clf)) for the shadow_blend lane

STATUS:    IMPLEMENTED — design + implementation in ONE PR (operator-directed
           same-day landing, 2026-07-27); additive kind, kind!=blend paths
           byte-identical (full suite green + explicit regression pins)
WHAT:      New `kernel/panel_pipeline/blend_scorer.py` (BlendPanelScorer +
           fail-closed two-pin loader) + `model_registry` kind "blend" +
           minimal dispatch wiring (LoadScorerTask path-anchor,
           ApplyScoresTask alpha158-branch routing with RAW-matrix
           pass-through, ResolveInferenceFramesTask / DriftGuardTask kind
           tuples, shadow_scoring `components` sub-config copy) so a shadow
           profile can run the certified blend objective as its PRIMARY
           scorer through the FULL decision funnel. Addendum (umbrella#535
           mirror): broker tag "alpaca_shadow_blend" added to the ONE
           `ALLOWED_BROKERS` source (`src/renquant_pipeline/state_paths.py`;
           the kernel copy imports it) so blend-lane components resolving
           state through the pipeline copy get an isolated
           `live_state.alpaca_shadow_blend.json`.
WHY/DIR:   The blend objective — `blend = z(prod_score) + z(clf_score)` per
           date, cross-sectional z (ddof=0) over the scored universe — is
           the certified construction of the renquant-model#74/75/76
           confirmatory line (prereg model#75; the clf artifact itself
           stamps `blend_spec: "z(prod_score) + z(clf_score) per date"`).
           The 2026-07-25 design (pipeline#213,
           doc/design/2026-07-25-blend-shadow-deployment.md) parked a
           shadow-slot + OFFLINE-readout mechanism pending that prereg;
           with the confirmatory line certified the operator directed a
           same-day landing of the blend as a first-class scorer KIND, so
           the shadow_blend lane exercises the full funnel (candidates →
           veto → QP → intents), not just an offline score join.

## Design (design section — this PR is design+impl in one)

1. **Dispatch.** `ranking.panel_scoring.kind = "blend"` resolves through the
   ONE kind registry (`model_registry.registry`), exactly like
   xgb/hf_patchtst/regime_router — both dispatch sites (primary
   `LoadScorerTask`, `shadow_scoring`) get it for free. Frozen config shape:

   ```jsonc
   "ranking": { "panel_scoring": {
     "enabled": true,
     "kind": "blend",
     "components": [            // exactly TWO, order-significant
       { "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json",
         "expected_content_sha256":     "sha256:<hex prefix ≥8 or full>",
         "expected_config_fingerprint": "sha256:<fp>"  /* or bare <fp> */ },
       { "artifact_path": "artifacts/shadow/panel-clf.top-decile.fwd60.json",
         "expected_content_sha256":     "sha256:<hex prefix ≥8 or full>",
         "expected_config_fingerprint": "sha256:<fp>" }
     ]
   }}
   ```

   Component 0 = production panel scorer (rank:pairwise xgb), component 1 =
   top-decile classifier (`panel_ltr_xgboost` payload). No top-level
   `artifact_path` is required: `LoadScorerTask` anchors its existing
   path-based surfaces (the STRICT config-consistency gate + the
   active-scorer trace stamp) on component 0 — the same production artifact
   the gate checks today.
2. **Identity pins — fail-closed at load, #211 digest rules.** BOTH keys are
   REQUIRED per component. Content: `_norm_digest`-style compare (strip
   optional `sha256:`, lowercase), ABBREV-TOLERANT prefix match (≥ 8 hex) —
   accepts both the 16-hex shadow_models pin convention and a full 64-hex
   pin. Config fp: VERBATIM compare tolerant of both written forms
   (with/without the `sha256:` prefix), no abbreviation. Any missing pin,
   mismatch, unresolvable path (via the ONE `artifact_resolver` authority),
   or history-requiring component ⇒ raise ⇒ `panel_scorer_load_failed`
   fail-close of the buy path.
3. **Interface parity.** `BlendPanelScorer` exposes the PanelScorer
   contract: `feature_cols` = sorted union, `requires_history=False`,
   `score(feature_matrix, ctx=None) -> pd.Series` (ctx accepted-ignored),
   KeyError on missing union columns. INPUT SPACE: `score` takes the RAW
   union matrix and applies EACH component's stored raw→model transform
   (`transform_feature_frame` with that leg's feature_means/stds)
   internally — the legs carry different normalization stats, so
   `ApplyScoresTask` skips its outer transform for kind blend and the
   composite metadata deliberately carries no stats.
4. **z guard (fail SOFT inside, recorded).** Per call, each leg is
   z-scored ddof=0 over its finite-scored universe; std==0 or <2 names ⇒
   that leg contributes 0 and `metadata.degraded_reason` records a token
   (`component<i>[<stem>]_std_zero` / `_n_lt_2`, reset each call). A name
   a healthy leg cannot score finitely gets NaN ⇒ dropped downstream as
   `panel_score_missing`. Load failures are NEVER soft (point 2).
5. **Metadata.** `kind="blend"`; `components` = both identities
   (resolved path, `sha256:`+full content digest, stored fp verbatim,
   trained_date, effective cutoff); `effective_train_cutoff_date` = the
   OLDER of the two legs (conservative staleness; None when either leg is
   unstamped — surfaced, not hidden; per-leg fallback = trained_date, the
   xgb convention); `config_fingerprint` = deterministic composite:
   `"sha256:" + sha256_hex(fp0 + "\n" + fp1)` over the STORED verbatim leg
   fps in config order (order-significant — the certified construction
   fixes 0=prod, 1=clf).
6. **No default-path change.** kind!=blend is byte-identical: registry
   registration is additive; the LoadScorerTask fallback only fires when
   `artifact_path` is absent AND kind=="blend" (previously an unconditional
   fail-close, which a regression test now pins for kind xgb); the
   ApplyScoresTask / feature-matrix kind tuples only gained the "blend"
   member; shadow_scoring only copies a `components` key when the shadow
   entry defines one.

EVIDENCE:
  artifact:      renquant-model#74/75/76 (certified confirmatory line;
                 prereg model#75) — the clf artifact
                 (`panel-clf.top-decile.fwd60.json`, re-stamped by
                 model#83 / strategy#67 with
                 effective_train_cutoff_date=2026-04-28) stamps
                 `blend_spec: z(prod_score) + z(clf_score) per date`
  prod or exp:   exp/shadow only — this PR ships the KIND; no profile or
                 pin flips here (shadow_blend profile + orchestrator job =
                 separate PRs; production primary config untouched)
  existing data: `make test` (umbrella venv, sibling common@origin/main):
                 2095 passed / 8 skipped (baseline pre-change: 2062 / 8,
                 same env) — 33 new tests in tests/test_blend_scorer.py
                 (dispatch, both-pin fail-closed matrix, hand-computed
                 ddof=0 z-sum, real-xgb parity incl. per-leg transform,
                 degenerate guards, interface parity, metadata/composite-fp
                 determinism + order sensitivity, kernel wiring + default
                 regression pins). Read-only smoke vs the REAL live
                 artifacts (prod 04d7a381… fp f8fb2259…, clf 6101a9fe… fp
                 1d8f167f…): loads, 172-col union,
                 effective_train_cutoff_date=2026-04-28 (older leg),
                 composite fp sha256:a2a061a0cb3fe652…, 12 names scored,
                 degraded_reason None; corrupted pin ⇒ ValueError.
  best-known?:   yes for the runtime mechanism — the blend recipe itself
                 remains governed by the model-side confirmatory line and
                 the #213 readout design; this PR adds no alpha claim
  scope:         "kind + scorer + minimal dispatch wiring ONLY — no config
                 flip, no pin advance, no launchd/run-surface change; the
                 shadow_blend profile + orchestrator readout/job land in
                 separate PRs through their own review paths"
NEXT:      (separate PRs) strategy/orchestrator: shadow_blend profile
           carrying this frozen config shape with the CURRENT certified
           pins (content pins are swap-sensitive: the 07-27 clf re-stamp
           already rotated 99687a90→6101a9fe; the composite fp is stable
           while leg fps hold) + orch job wiring/launchd via the
           run-surface review path. Preflight `staleness` rail currently
           soft-skips unrecognized kinds incl. blend — acceptable for the
           shadow lane (staleness surfaced via metadata
           effective_train_cutoff_date); extend the rail when the lane
           graduates.

## Fix (2026-07-28, review findings addressed)

WHAT:      `LoadScorerTask.run()`'s component-0 path anchor for
           `kind="blend"` only fired in the fresh-load branch
           (`ctx._panel_scorer is None`). The documented preloaded branch
           (adapter / LEAN calling `LoadScorerTask.run()` with
           `ctx._panel_scorer` already set) skipped it, so
           `_resolve_artifact_path` returned `None` (composite metadata
           has no top-level `artifact_path`) and `_assert_config_consistency`
           checked the composite fingerprint instead of the component-0
           production fingerprint — fail-closing a valid preloaded blend
           scorer as `panel_scorer_config_mismatch`. Extracted the anchor
           into a shared `LoadScorerTask._blend_component0_path()` static
           method and call it from both branches
           (`src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py`).
EVIDENCE:  artifact:      tests/test_blend_scorer.py::TestKernelWiring::
                          test_load_scorer_task_dispatches_preloaded_blend_without_top_level_path
           prod or exp:   exp (unit test, no config/pin/artifact change)
           existing data: confirmed the test fails on pre-fix code
                          (`panel_scorer_config_mismatch`, skip_buys=True)
                          and passes post-fix. `tests/test_blend_scorer.py`:
                          38 passed (was 37). Full suite (venv
                          `renquant-pipeline/.venv`, PYTHONPATH to sibling
                          `renquant-common`/`renquant-base-data`/
                          `renquant-artifacts` src): 2092 passed / 9
                          skipped, same 2 pre-existing failures in
                          `tests/test_replay_d6_conventions.py`
                          (HAC t-stat null vs pinned value — reproduced
                          identically on the unmodified pre-fix head, so
                          unrelated to this change) both before and after
                          the fix.
           best-known?:   yes — closes the only reviewer-identified gap
           scope:         "LoadScorerTask preloaded-branch anchor fix +
                          regression test only; no config/pin/artifact
                          change"
NEXT:      none — addresses both CHANGES_REQUESTED reviews on this PR.
