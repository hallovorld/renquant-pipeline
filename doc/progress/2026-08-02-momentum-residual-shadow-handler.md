# momentum_residual shadow serving handler — slice 4b of the momentum pipeline (model#197 F-1)

STATUS: planned — code + tests complete on this branch; the handler is INERT
until (and unless) the slice-5 grant batch lands. Batch slot per model#197
amendment 2 (build-order): slice 3 (evaluator) → **this slice 4b** → 4c
(umbrella gate rule) → the one grant: install job → first artifact publish →
merge s104#77 → pin advance. This PR must MERGE AND PIN before the batch's
pin advance; nothing reads the new kind until the s104 `shadow_models` entry
exists on the pinned config.
NEXT: land this PR (codex approval) → advance the renquant-pipeline pin —
BOTH before the grant batch's pin advance, per model#197 amendment 2's
revised order (… → 4b → 4c → grant). Slice 4c (the umbrella gate rule for
ledger-pointer entries) is the next build item after this merges; the grant
batch itself remains operator-gated and does not start from this repo.

WHAT: registers kind `momentum_residual` in the panel model registry and
implements the serving contract declared by s104#77's narrative key (the F-1
blocker recorded on model#197 amendment 2 — without it, the daily shadow load
fault-records on the momentum lane every run after the batch):

- `kernel/panel_pipeline/momentum_residual_scorer.py` (new): the configured
  `artifact_path` is the append-only digest-chained LEDGER
  (`artifacts/momentum/momentum_artifact_ledger.jsonl`, the one cutoff-stable
  file in the weekly publish set). The loader verifies the FULL ledger chain —
  `renquant_model_momentum.load_and_verify_ledger`, imported, never
  reimplemented — takes the TAIL row, loads the dated artifact beside the
  ledger (`<cutoff>/momentum_residual_v0.json`, the model#197 decision-1
  convention), verifies the artifact's self-carried `content_sha256`
  (package verifier) AND the row's pin over it, cross-checks row↔artifact
  parity (kind / cutoff_date / params_version), then reproduces the composite
  from the stored features via the package's own
  `renquant_model_common.momentum_features.composite_scores` and requires
  agreement with the stored scores (digests verify identity, not validity —
  this is the paired golden reproduction). Serving = per-ticker lookup into
  the verified score set (`scores_by_ticker` capability; no feature matrix,
  no history panel).
- FAIL-CLOSED: every verification refusal raises with a distinct grep-able
  prefix that lands verbatim in the health record's `load_error`
  (`ledger_chain_verification_failed:` / `dated_artifact_missing:` /
  `dated_artifact_unparseable:` / `artifact_content_sha_mismatch:` /
  `ledger_row_artifact_sha_mismatch:` / `artifact_*_mismatch:` /
  `scores_reconstruction_mismatch:`) → `load_failed` FAULT, never fatal to
  the primary path.
- The ONE non-fault refusal: an EMPTY (or resolved-but-missing) ledger — the
  designed PENDING_FIRST_ARTIFACT window — raises `ShadowNotYetPublished`,
  which `ApplyShadowScoringTask` catches SPECIFICALLY and stamps as the new
  `not_yet_published` EXPECTED-skip state (additive to
  `shadow_scorer_health.v1`; no schema bump — the deployed sentinel
  constrains `status` to the canonical three and requires `state` only to be
  a string `[VERIFIED — orchestrator
  ops/renquant104/rq104_shadow_scorer_sentinel.py::is_valid_v1_record]`).
- Staleness surface: the record's `effective_train_cutoff_date` is stamped
  from the TAIL ROW's `cutoff_date` (the weekly publish cadence the sentinel
  should watch), NOT the artifact's measured input cutoff, which trails the
  cutoff by the ~21-business-day skip embargo BY CONSTRUCTION and would flag
  a same-day publish stale on arrival (the fwd60 stale-on-arrival class,
  pipeline#220). The measured value stays visible as
  `artifact_effective_train_cutoff_date`. Deliberately NO `lookahead_days`
  declared: v0 trains on no forward label, so the single-axis 28-day rule
  over the cutoff date is the correct gate for a weekly cadence.
- `config_fingerprint` (required identity, absent from the momentum artifact
  per s104#77 F-2): stamped as `momentum-<params_version>-<sha256(canonical
  params)[:16]>` — recomputable from the artifact by any reader.
- Cross-repo import approach: the established renquant-model precedent —
  GUARDED import (hf_patchtst idiom) + a new `[momentum]` optional extra
  (`renquant-model>=0.2.1,<0.3`) in `pyproject.toml`; tests importorskip the
  model packages exactly like the hf_patchtst suites (pipeline CI checks out
  common/base-data/artifacts but NOT renquant-model; locally the sibling
  checkout on the pytest pythonpath provides them). A missing distribution at
  serving time = a FAULT record NAMING the dependency + remedy, never a crash.
- Scorer-cache key now includes the certified content digest
  (`(kind, path, content_sha256)`): the ledger keeps ONE stable path whose
  bytes advance on every weekly append; a path-only key would pin the
  first-loaded tail for the life of a long sim process. The digest is already
  in hand from the single canonical resolution (no extra hashing); two
  existing tests that pre-seeded the old 2-tuple key updated to the new
  contract.

REVIEW ROUND 1 (codex CHANGES_REQUESTED, two blockers — both fixed here):
- TOCTOU (blocker 1): the task certified the ledger digest, then the loader
  REOPENED the live path — a weekly append between the two reads would serve
  the NEW tail under the OLD certified digest. Fix = single-read closure:
  the loader reads the live path's bytes EXACTLY ONCE, derives the consumed
  digest and the chain verification (over a private snapshot of those same
  bytes — still the package's verifier, which takes a path) from that one
  snapshot, and exposes `metadata["consumed_content_sha256"]`. The task
  compares it to the certified identity BEFORE caching or marking loaded:
  on divergence it re-certifies ONCE (same resolved file, digest must equal
  the consumed bytes — the benign append race then serves the new tail
  under its OWN certified identity, health fields updated to match), else
  refuses with an `artifact_identity_divergence:` FAULT, nothing cached.
  Deterministic regressions: append injected between certification and load
  (must re-certify, never mix); a resolver pinned to the stale identity
  (must fault, cache empty).
- CI vacuous-green (blocker 2): the suite importorskipped
  `renquant_model_momentum` and pipeline CI has no model checkout, so
  required CI proved only the absent-dependency path. Fix = the backtesting
  precedent: `.github/workflows/ci.yml` now checks out
  `hallovorld/renquant-model` and source-installs it (`pip install -e
  renquant-model`), plus a fail-closed guard step that (a) imports
  `renquant_model_momentum` (fails the job if the model package ever drops
  off the CI path) and (b) runs the serving-boundary suite explicitly —
  the module-skip condition is now impossible in the required check while
  the importorskip stays for machines genuinely without the distribution.
- Also added this doc's previously-missing top-level `NEXT:` field.

WHY/DIR: model#197 amendment 2, F-1 — the grant batch cannot advance the pin
before this handler exists, or the daily shadow load fault-records on the
momentum entry from day one. The lane inherits every GOAL-1 silent-death
guard for free precisely because it goes through the standard
`ApplyShadowScoringTask` health-record path (design §3 TRADE, model#195).

EVIDENCE:
  artifact:      tests/test_momentum_residual_shadow_handler.py
  prod or exp:   exp — merge-inert: kind `momentum_residual` is dispatched
                 only when a configured `shadow_models` entry names it, and
                 that entry (s104#77) merges only inside the slice-5 batch;
                 the `not_yet_published` state + digest-keyed cache are
                 exercised only through the same shadow task, which is
                 fail-soft by construction
  existing data: s104#77 body (F-1/F-2/F-3 findings + the declared serving
                 contract); model#197 amendment 2 (batch order);
                 model tools/momentum_train_run.py (publish layout: dated
                 artifact + LEDGER_BASENAME)
  best-known?:   yes — implements the declared contract with the package's
                 own chain/sha/construction functions; the only alternative
                 (reimplementing chain math pipeline-side) is the never-copy
                 violation the design forbids
  scope:         "this is tests/test_momentum_residual_shadow_handler.py
                 (18 tests, real package writers, real registry + resolver)
                 + the full pipeline suite, exp path (inert until the s104
                 entry exists AND the pin advances), vs baseline =
                 origin/main 398cda9"

  Measured counts (post review round 1): new suite **18 passed** (15 + the 3
  single-read-closure regressions) `[VERIFIED — pytest -q
  tests/test_momentum_residual_shadow_handler.py, this branch, 2026-08-02]`.
  Touched shadow suites (health record, shadow health, artifact resolution,
  coverage counts, degenerate-matrix guard + the new suite) **67 passed**
  `[VERIFIED — pytest -q, same session]`. Full suite baseline at origin/main
  398cda9: **2 failed, 2356 passed, 9 skipped** (the 2 = the known
  pre-existing platform failures in `test_replay_d6_conventions`
  pin-platform byte-identity) `[VERIFIED — make test in this worktree,
  pre-change]`; post-fix: **2 failed, 2374 passed, 9 skipped** — same 2,
  zero regressions `[VERIFIED — make test, post-change]`. CI execution of
  the suite (not skipped): enforced mechanically by the new workflow guard
  step; the post-push CI run is the proof surface.

TEST MAP (each failure path its own distinct record):
  happy path scores + healthy record (no monkeypatched registry/resolver);
  tail-row (not first-row) selection; empty ledger → `not_yet_published`
  expected-skip (loader raise + task record + finalize passthrough); chain
  tamper → `ledger_chain_verification_failed`; missing dated artifact;
  edited artifact → self-sha; self-consistent swap → row-pin mismatch;
  fabricated scores (self-consistent AND honestly ledgered) →
  reconstruction mismatch; blocked import → fault naming
  `renquant_model_momentum` + the `[momentum]` remedy; primary candidates
  byte-identical under a faulting lane (record-don't-raise control); weekly
  append busts the digest-keyed scorer cache within one process; loader
  metadata carries the consumed digest; append injected between
  certification and load → re-certify, never new-bytes-under-old-identity;
  stale-pinned resolver → `artifact_identity_divergence` fault, cache empty.

DEPLOYMENT NOTE (for the grant batch, not this PR): the daily runner's
environment must make `renquant_model_momentum` importable (sibling
renquant-model checkout on the path or the `[momentum]` extra). If it is
absent post-batch, the lane's designed failure mode is a daily
`load_failed` FAULT record naming the dependency — visible to the sentinel,
harmless to the primary path — until the environment is fixed.
