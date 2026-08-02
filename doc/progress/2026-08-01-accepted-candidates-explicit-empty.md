# accepted_candidates holds on every exit: explicit empty, never a missing attribute (#246)

## Root cause `[本次实测 2026-08-01]`

The main-red test (`test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`)
died on `AttributeError: accepted_candidates` because the #219 UNIT GUARD fires on its
fixture: no calibrator → `panel_scores` in the RAW domain vs a probability-domain buy
floor → `VetoWeakBuysTask` correctly REFUSES the unit-mismatched comparison
(`rank_score_domain_uncalibrated`) and returns before the acceptance loop. The guard is
right; the contract was the defect: every `_block_all` exit left `accepted_candidates`
UNSET, and downstream readers (`panel_scoring.py:658`, `selection.py`) use
`getattr(ctx, "accepted_candidates", []) or []` — an unset attribute is
indistinguishable from "zero accepted", the silent-empty shape this program has now hit
seven times.

## Fix

* `_block_all` sets an EXPLICIT `accepted_candidates = []` when absent (guarded so a
  per-ticker acceptance pass is never clobbered) — the reason stays in
  `blocked_by`/`buy_blocked`. Every one of the seven `_block_all` call sites now
  honours the acceptance contract on exit.
* The test is rewritten to the CURRENT contract: scoring unaffected, admission blocked
  with `rank_score_domain_uncalibrated`, `accepted_candidates == []` explicitly,
  no order intents. (Its pre-guard assertion — AAPL accepted from raw scores — is the
  exact unit bug the guard exists to prevent; asserting it again would re-litigate
  #219.)

Suite: **2301 passed, 8 skipped, 0 failed** — main's standing red test is green again
`[本次实测]`.
