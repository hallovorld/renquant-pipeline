# Decision-schedule record-validation contract — G4 step 1

Date: 2026-07-18
Design source: renquant-model#61 — `experiments/ensemble_phase0/
DESIGN_AMENDMENT_v4_executable_next_open_evaluation.md` (§2 canonical
next-open observation; §5 ownership + implementation order, step 1:
"Specify and test the public pipeline schedule/validation contract with
fixture vectors").

## What

- **`renquant_pipeline.decision_schedule` (new, versioned
  `DECISION_SCHEDULE_CONTRACT_VERSION = 1`)** — the public
  record-validation API for the v4 §2 next-open observation:
  `validate_arm_record` / `validate_session_records` returning
  structured `ScheduleVerdict` / `SessionVerdict`
  (`ok`, stable `reason_codes`, `failure_class`, `evidence_flags`),
  plus `job_identity` (deterministic over `{arm, T, artifact digests,
  config digest}`; canonicalization pinned byte-equal to
  `renquant_artifacts.contracts.hash_jsonable` by test, without
  importing it) and `recompute_watermark_from_manifest` (the reference
  watermark recomputation; production admission injects the
  digest-resolving hook in step 2).
- **Fixture vectors** `tests/fixtures/decision_schedule/vectors.json`
  (+ regenerator): valid pair, byte-identical retry, late watermark,
  divergent retry, missing arm, shared price-source failure, asymmetric
  valuation failure, timestamp-outside-window (evidence flag, not
  failure). Digests are real sha256 values from the repo's shared
  hashing utilities. Promotion of the vectors to renquant-common is a
  LATER step, mirroring the bundle_contract precedent (pipeline#206 →
  common#32).
- **Tests** `tests/test_decision_schedule.py` (54): all vectors with
  per-arm blame attribution, job-identity determinism/sensitivity/
  hash-utility pin, retry byte-identity (divergence never resolved by
  latest-commit, order-independent), the injectable watermark
  recompute hook (mismatch/None fail closed; equality is
  instant-based), import-lightness in a subprocess (stdlib ONLY —
  stricter than bundle_contract), and fail-closed edges (unknown
  schema, naive timestamps, wrong-session orders, tampered job id,
  unexpected arm, mixed sessions, frozen-identifier mismatch,
  at-close boundary).

## Semantics pinned where v4 §2 is silent (reported, not improvised)

- Run-bundle timestamp outside `(close(T), open(T+1))` is an EVIDENCE
  flag on an otherwise qualifying record, never an admission failure
  (v4: "timestamp is evidence only"; the §2 bullet list also names the
  window — the dispatch prompt for this step fixed the flag-not-failure
  reading).
- A symmetric declared failure (both arms, same kind in
  `SHARED_FAILURE_KINDS = {price_source, venue_outage,
  calendar_outage}`) classifies `shared` (B_shared) even if other
  admission codes coexist; every other inadmissible outcome — including
  both arms failing with different kinds, and symmetric ADMISSION
  failures (e.g. both late) — classifies `idiosyncratic` (B_idio).
- A crash-then-success retry: the qualifying record governs; the failed
  attempt is kept as the `failed_attempt_recorded` evidence flag.
- Watermark boundary: `declared == close(T)` qualifies ("no later
  than").
- Classification is OUTPUT only; budget sizes/counting and the terminal
  `NO-GO (integrity)` consequence stay in the runner (step 2).

## Boundaries

- Nothing here schedules, trades, or touches any run surface; pure
  validation + fixtures.
- Calendar resolution (`calendar_id` → close/next-open instants) is the
  caller's job via `renquant_common.market_calendar` — the module stays
  stdlib-only so the model repo's backfill (step 3) can import it
  without the runtime stack.
- Steps 2–4 (orchestrator canonical job + admission ledger, model
  backfill/PIT-ledger consumption, pinned umbrella integration run)
  follow after review, per v4 §5.

## Verification

- `make test`: full suite green except the 3 pre-existing
  environment-dependent failures (`test_replay_d6_conventions` byte-pin
  x2, `test_xgboost_scorer_contract` real-artifact scoring) — identical
  set fails on unmodified `main` in the same environment; 54/54 new
  tests pass.
