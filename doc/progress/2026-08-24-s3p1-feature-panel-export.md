# S3-P1: persist the served daily feature panel

STATUS:   delivered — first implementation PR under the approved orch#1026
          design (rq105 Stage-3, operator-directed "105 = live trade").
WHAT:     `kernel/panel_pipeline/feature_panel_export.py` — after the primary
          scorer's matrix is final, the prod lane writes
          `data/rq105/feature_panel_<date>.json` (+ meta with content sha256),
          keys mirroring the orchestrator's `FeatureSnapshot.from_mapping`
          contract (feature_cutoff / builder_version / non-empty features).
          Wired directly after `ApplyScoresTask` in `PanelScoringJob`.
WHY/DIR:  nothing persists the served feature vectors (verified 2026-08-23:
          no feature file anywhere under data/). This one absence blocks the
          rq105 snapshot producer (`SKIP not-wired` daily since 08-12),
          post-hoc score attribution (#17), and G-K panel sharing. S3-P2 (the
          intraday snapshot producer) consumes this file next.
EVIDENCE:
  artifact:      the module, 12 hermetic tests, the ci.yml wiring.
  prod or exp:   exp — merged code is inert for capital: observe-only export,
                 fail-open (a writer explosion logs WARNING; scoring runs on),
                 atomic writes.
  existing data: guards are measured, not guessed — every intraday run has
                 n_candidates=0 (per-run DB measurement), hence the
                 candidate-less skip; readonly lanes share the date-keyed
                 filename, hence the RENQUANT_READONLY_TAG skip.
  best-known?:   yes — exports the matrix the scorer ACTUALLY consumed
                 (ctx._panel_matrix), not a recomputation that could diverge.
  scope:        one new module + one task-list line + tests + ci.yml. No
                config, no gate, no order path. The contract is mirrored, not
                imported (repo boundary).
  NOT DONE:     the snapshot producer (S3-P2), wiring run_shadow_serving
                (S3-P3), the entry loop (S3-P4). Strictly this PR.
REVIEW:    codex (haorensjtu-dev).
