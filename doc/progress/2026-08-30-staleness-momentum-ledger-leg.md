# orch#906: P-MODEL-STALENESS reads momentum ledger legs

STATUS:   delivered. `staleness.py` registers `momentum_residual` in BOTH the
          solo dispatch and the blend-leg reader: dates come from the
          chain-VERIFIED ledger tail row via a new public
          `momentum_residual_scorer.verified_ledger_tail_row` (single-read
          snapshot + the model package's own `load_and_verify_ledger` —
          never reimplemented). Axis mapping: `trained_date` <-
          `appended_at_utc` (the weekly train job appends at publish time,
          so the append stamp IS the retrain clock); `effective_train_cutoff_date`
          <- `cutoff_date` (the serving contract's DECLARED staleness
          surface — the artifact's own measured cutoff trails it by the skip
          embargo BY CONSTRUCTION and would flag a same-day publish stale on
          arrival, the fwd60-stale-on-arrival class PR #220 fixed).
WHY/DIR:  the LIVE prod z-blend's slow-momentum leg surfaced every run as
          "component[1] kind='momentum_residual' is not a staleness-readable
          leg kind" (`staleness.py` blend gap; orch#906) — the blend's
          freshness rail could never establish its binding axis, and the
          daily model-freshness readout carried the same gap. The
          registration is the designed extension point the gap text itself
          names ("register the kind").
EVIDENCE:
  artifact:      `verified_ledger_tail_row` (+`__all__`),
                 `staleness._momentum_ledger_meta`, solo + blend branches;
                 `tests/test_preflight_staleness_momentum.py` (8 tests over
                 the REAL chain machinery: readable leg, stale-cutoff binds,
                 tampered chain = surfaced gap, empty ledger =
                 PENDING_FIRST_ARTIFACT gap, missing ledger gap, solo rails,
                 constant-mirror pin). Existing staleness + blend momentum +
                 shadow-handler suites green [VERIFIED — pytest run
                 2026-08-30].
  prod or exp:   exp — LIVE behaviour changes only after merge + the
                 pipeline pin advance; until then the daily run keeps
                 printing the surfaced gap (fail-closed, safe).
  existing data: strategy_config components[1] pins the ledger pointer
                 (`artifacts/momentum/momentum_artifact_ledger.jsonl`)
                 [VERIFIED — config read 2026-08-30]; ledger rows carry
                 `appended_at_utc`/`cutoff_date`/`effective_train_cutoff_date`
                 by the `_ROW_REQUIRED` contract (renquant-model ledger.py).
  best-known?:   yes — reuses the one chain verifier and the declared
                 serving-contract axis; fail-closed on every fault class
                 (unreadable, broken chain, empty, undated row, missing
                 model distribution).
  scope:        one public helper in the scorer module, one reader + two
                dispatch branches in staleness.py, tests. SOFT severity
                unchanged; no scoring-path behaviour touched.
RELATED:   renquant-model `fix/panel-data-cutoff-stamp` +
           renquant-orchestrator `fix/rq104-freshness-data-cutoff` (the other
           two legs of orch#906); this PR is independent of their merge
           order.
REVIEW:    codex (haorensjtu-dev).
