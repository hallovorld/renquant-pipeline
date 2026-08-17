# Provenance-required cutover — migrate the 7 artifact-manifest fixtures

STATUS:    delivered. Test-only, no src change, no behaviour change, nothing
           deployed, no live path touched. Restores the suite, which is red on
           every push since 2026-08-15: 28 failed -> 0.

WHAT:      7 manifest fixtures across 6 test files:
           * `+ "provenance": {"kind": "none"}` — now a REQUIRED field;
           * `"promotion_status": "prod"` -> `"candidate"` — `kind="none"`
             cannot be combined with `prod`.
           Exactly 14 insertions / 7 deletions, all of the two shapes above.

WHY/DIR:   `renquant-artifacts` sets `PROVENANCE_REQUIRED_AFTER = date(2026,8,15)`;
           `provenance_required()` returns True unconditionally on/after that
           date (one-way, no env override). A scheduled cutover that arrived,
           NOT a regression.

           Each fixture claimed `promotion_status="prod"` while carrying no
           lineage at all. The guard rejecting that is the guard working: a real
           prod artifact must resolve a canonical, registry-published lineage
           (`publication_record_digest` + registry bindings). These are synthetic
           test doubles, so `"prod"` was a fiction the cutover exposed rather
           than a capability it removed, and `kind="none"` is the accurate
           determination for them.

           Rejected alternative: keep `"prod"` and satisfy the canonical path.
           That means fabricating a registry publication record for a test
           double — manufacturing exactly the evidence the guard exists to
           demand.

LIVE IMPACT: none observed. `ValidateRuntimeInputsTask`
           (`src/renquant_pipeline/inference.py:93`) validates the SERVING
           artifact manifest, so the failure mode would be a live fail-close.
           It is not occurring: the 2026-08-17 daily run reached decisions, and
           `ValidateRuntimeInputs` appears ZERO times in its log — the live path
           runs the kernel twin (`kernel.config_schema`, `adapters.panel_runtime`),
           not this module. Stated at its true strength: this is log-absence plus
           a completed run plus the known twin split, not a positive trace of the
           kernel path skipping validation.

EVIDENCE:
  artifact:       7 fixtures in tests/test_{intraday_decisioning,
                  panel_scoring_contract,selection_contract,
                  xgboost_scorer_contract,runtime_features,inference_pipeline}.py
  prod or exp:    neither — test fixtures; no src consumer
  existing data:  main's last CI run is 2026-08-12, BEFORE the cutover date, so
                  "last green" is not evidence of health. Measured locally on a
                  clean origin/main sibling worktree instead.
  best-known?:    yes. `promotion_status` has NO code consumer in
                  `renquant-pipeline/src` — the only 2 occurrences are docstring
                  text (kernel/portfolio_qp/tasks.py:882, :2220) — and no test
                  asserts on it, so the changed value is semantically inert.
                  The two `pytest.raises(..., match="non-accepted")` selection
                  tests cannot pass vacuously on a different ValueError: the
                  match anchors the reason.
                  Validator behaviour measured directly, not inferred:
                    kind=none      + prod       -> REJECT
                    kind=none      + candidate  -> PASS
                    kind=canonical + prod       -> REJECT (missing
                                                   publication_record_digest,
                                                   registry bindings)
                    kind=canonical + candidate  -> PASS
  scope:          this repo's fixtures only. The same cutover independently
                  breaks renquant-model (PR #226, which also fixes a real src
                  defect), renquant-backtesting (PR #113) and
                  renquant-orchestrator; each filed in its own repo.

VERIFICATION:
  Run from a SIBLING worktree — `[tool.pytest.ini_options] pythonpath` uses
  `../renquant-*/src`, so a worktree outside `git/github/` fails with unrelated
  ModuleNotFoundErrors.

  pre-fix  (clean origin/main): 28 failed, 2589 passed, 8 skipped
  provenance added, prod kept:  28 failed   <- proves BOTH edits are needed
  post-fix:                     2617 passed, 8 skipped

OBSERVED, NOT FIXED (out of scope, no change made):
  `tests/test_selection_contract.py` —
  `test_selection_rejects_promoted_candidate_not_in_alpha_set` and
  `test_selection_rejects_blocked_candidate_even_if_manually_selected` have
  BYTE-IDENTICAL bodies. The second names a "blocked candidate, manually
  selected" scenario it never constructs, so that scenario is currently
  untested. Flagged for a separate PR rather than widened into this one.

NEXT:      merge to restore main. No follow-up needed in this repo; the
           governance question about what REAL trained models declare is carried
           in renquant-model PR #226.
