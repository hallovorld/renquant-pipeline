# Progress: eligibility-ledger precondition — implementation (amendment #207)

Date: 2026-07-18

## What

Implements the approved r2 amendment
`doc/design/2026-07-18-smalln-guard-eligibility-ledger.md` (merged #207;
base RFC #204, stage-1 mechanism #205) plus the approving review's two
implementation expectations: (a) the POLICY set-identity assertion and
(b) the config-frozen watchlist outer anchor in the §3 block. Mechanism
only — production and golden configs carry NO guard keys (strat-104#61);
nothing changes for prod (bit-identity pinned by test).

- **New module `kernel/smalln_eligibility.py`** (declared in
  `OWNED_KERNEL_STEMS`): partition builder, CLEAN predicate, suppression
  logging, §3 block assembly. Single-source — imported by BOTH twins so
  the predicate cannot drift (the verbatim-lockstep contract stays scoped
  to the floor helpers). Placed at kernel top level, NOT inside
  `panel_pipeline`, because `pp_inference` must emit the generation
  counter and is contractually barred from importing `panel_pipeline`
  (`test_lift_pp_inference` ownership rule).
- **Generation-stage instrumentation (§2 condition 1, required):**
  `pp_inference.py` Phase 2b emits `ctx.counters["expected_universe"]` +
  the ticker list (`ctx._smalln_expected_universe_tickers`) immediately
  after `_buy_universe(ctx)`, BEFORE any drop. Absent counter (older
  pipeline, skipped scan) → NOT CLEAN by definition.
- **CLEAN predicate (§2, evaluated EVERY session, gates only the small-n
  branch):**
  1. mass balance — set accounting of every expected-universe name into
     survivor-at-floor / per-name recorded exclusion / scan-drop, plus
     the counter arithmetic cross-check; unaccounted shortfall, scan
     surplus, counter/list inconsistency, or an absent counter → NOT
     CLEAN;
  2. funnel integrity — `panel_score_missing == 0` and zero NaN/inf
     rank_scores;
  3. allowlist + share bounds — built-in INTEGRITY map (`wash_sale:*` →
     wash_sale ≤ 20%, `risk_gate_vol` → realized_vol ≤ 50%,
     `earnings_blackout` → corporate_action ≤ 10%; breach strictly `>`),
     config-frozen under `ranking.panel_scoring.smalln_eligibility`
     (`integrity_share_bounds`, `integrity_reasons` extensions,
     `policy_reasons`); unknown reason → NOT CLEAN; POLICY reasons are
     exempt from bounds but every DECLARED policy reason is asserted by
     set identity (exact string; tagged set == watchlist − declared
     eligible set) against the full blocked map — POLICY narrowing is
     applied upstream of generation, so its tags live outside the
     emitted universe;
  4. failure markers — `_panel_scoring_contract_failed`,
     `_calibrator_contract_failed` (both carry the fingerprint-dispatch
     route in their reason on fingerprint mismatches),
     `panel_score_missing`, `veto:rank_score_nan`, and the PROMOTED
     `_feed_staleness_flagged` (the `_apply_fund_features` staleness
     warning now also stamps a machine-readable ctx marker; warn-only
     behavior unchanged).
- **Wiring in `VetoWeakBuysTask` (kernel) ahead of the stage-1 branch:**
  partition built at task entry on every session; branch acts ONLY when
  CLEAN; NOT CLEAN at small n → status-quo floor +
  `smalln_guard_suppressed(reason=<first failing class>)` at ERROR +
  recorded in the block. `branch_action` ∈ {acted, not_small_n,
  suppressed:<reason>, deconfigured}; no-floor paths (empty scan,
  buy_floor unset, sim bypass, absolute mode) record `deconfigured`
  unless a validly-configured guard meets a small NOT-CLEAN partition —
  then the suppression is still recorded loud (the AC-F limiting shape).
- **§3 persistence:** schema-versioned block (`schema_version: 1`)
  attached as `ctx._smalln_eligibility` — full partition, watchlist_size
  outer anchor, finite_n, n0, original/relaxed floors (equal unless
  acted), branch_action, suppressed_reason, candidate_delta — and
  forwarded through BOTH existing write paths without touching the
  orchestrator: a gate-registry `smalln_eligibility` submission (verdict
  `allow`, lattice-neutral) that the adapter's `record_gate_verdicts`
  persists to the runs DB, and a `format_gate_verdicts` row
  (`decision_ledger.py`) for the decision-ledger write. The run-bundle
  copy is the orchestrator's side (`build_bridge_live_bundle`), reading
  the same ctx attribute, absent-tolerant — separate orchestrator PR per
  the amendment's named deliverable.
- **Twin (`panel_scoring.py`):** `_guarded_smalln_floor` applies the same
  CLEAN gate via the shared module. The twin's simplified contract
  normally has no generation counter → its small-n branch now suppresses
  fail-closed (AC-D) unless the driving harness emits the counter; the
  stage-1 fixtures were updated to seed a CLEAN partition, which is the
  07-16/07-17 motivating shape (universe == scanned == 5).
- **Superseded stage-1 expectation:** NaN residue at small n no longer
  relaxes — `test_nan_scores_excluded_from_guard_n` re-pinned to the
  amendment behavior (suppressed, funnel_integrity class); NaN exclusion
  from the guard's n is still asserted via the partition record.

## Known accounting gap (stated)

`PostStopCooldownFilterTask` (opt-in, default OFF) drops candidates with
a counter but no per-name blocked record; if enabled and firing at small
n those names are unaccounted → NOT CLEAN. Fail-closed by design: a
surface that wants approved-normal status must first record per-name
evidence and then be allowlisted in reviewed config.

## Tests

`tests/test_smalln_eligibility_ledger.py` (new, 34): AC-A (recorded
07-16/07-17 CLEAN → acted, delta exactly {ATI, EME, BWXT}, anchor =
145), AC-B (score_missing residue → suppressed + LOUD), AC-C pipeline
side (block on acted / suppressed / not_small_n / deconfigured / empty /
floor-unset paths; registry + ledger rows; absent-tolerant formatter),
AC-D (absent counter; unknown reason; every failure marker enumerated),
AC-F (expected 145 / entered 5 / zero exclusions → suppressed while all
within-funnel records look healthy), AC-G (share above / exactly at the
bound; config-frozen override; invalid override ignored loudly), POLICY
set identity (holds / missing tag / wrong subset / malformed
declaration), bit-identity with guard keys absent (floors, kept set, no
ERROR logs), feed-staleness promotion (stale / fresh / no-ctx
back-compat), emission helper, twin fail-closed + twin acted, and
first-failing-class precedence. Full suite: 1881 passed, 9 skipped,
1 pre-existing env-dependent failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
identical on unmodified main — local xgboost version, green in CI).

## Not in this PR

Sentinel `smalln_guard_suppressed` LOUD pattern + run-bundle write
(orchestrator PR, the amendment's named deliverable); strategy-104
shadow-config keys (strat-104#61); §4 shadow experiment execution;
production activation (gated on the frozen §4 verdict + explicit
operator authorization + a pin PR superseding RenQuant#498).
