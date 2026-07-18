# Progress: bundle contract phase 2 — public pair-validation API

Date: 2026-07-18

## What

Phase 2 of the GOAL-5 AC4 bundle-transactionality RFC (RenQuant#492,
`doc/design/2026-07-17-artifact-bundle-transactionality.md` §2.5): the
renquant-pipeline PUBLIC pair-validation API the transactional bundle
store (phase 1, renquant-artifacts#25, MERGED) invokes at writer-protocol
step 6 before publication. Binding the artifacts store to this validator
in production wiring — and promoting the contract fixtures to
renquant-common — is phase 3, not this PR.

- **`renquant_pipeline/bundle_contract.py` (new, versioned
  `BUNDLE_CONTRACT_VERSION = 1`)**: `validate_pair(manifest,
  member_paths) -> PairVerdict`. Structured verdict for the store seam
  (`ok` bool — falsy ⇒ reject, per `BundleStore._run_pair_validator`;
  stable `reason_codes`; `matched_schema` = the ONE schema the pair
  matched under, `"v1"`/`"legacy"`). Never raises on a validation
  outcome. Pair CONSISTENCY only (RFC §2.7 — not a WF-gate/admissibility
  statement); `manifest` is consulted for `schema_version` + the
  member-name→role mapping only (digests/field closure are the store's
  schema checks; pinning `bindings` content is phase 3).
- **Shared-helper refactor (no divergent copy)**: the runtime loader's
  matcher (`kernel/panel_pipeline/job_panel_scoring.py::`
  `_assert_calibrator_matches_scorer` — the 05-27/06-22/07-01/07-14→16
  incident site) had its claim construction moved verbatim into
  `kernel/panel_pipeline/fingerprint_dispatch.py`
  (`fingerprint_values_from_metadata`, `scorer_claim_from_metadata`,
  `calibrator_claim_from_metadata`, keys pinned in
  `METADATA_IDENTITY_KEYS`); the runtime path re-binds the historical
  private names to the SAME functions (is-identity-tested), inputs and
  error messages unchanged — bit-identical, all pre-existing dispatch/
  calibrator tests pass unmodified. The M6 schema-dispatch rule
  (legacy-vs-v1 compared within ONE schema only, never across) is
  therefore enforced identically at serve time and at publication time.
  The WF loader's narrower calibrator key set is a pre-existing,
  deliberate divergence and is untouched.
- **Import-lightness**: `renquant_pipeline/__init__.py` converted to the
  PEP 562 lazy pattern already used by `kernel/panel_pipeline/__init__.py`
  (same rationale: importing any submodule runs the package `__init__`).
  Public surface (`__all__`, attribute access, submodule access,
  `TYPE_CHECKING` imports for static analysers) unchanged; eager heavy
  imports (pandas/numpy/scipy/renquant_artifacts, ~1.7s) no longer run
  on `import renquant_pipeline.bundle_contract` (~0.5s). Residual:
  pandas/numpy/scipy still arrive via `renquant_common.__init__` (eager
  in the shared dep renquant-artifacts already declares) — fixing that is
  a renquant-common change, out of scope here; the import-lightness test
  documents the allowance.
- **Contract fixture vectors**: promoting fixtures to renquant-common
  requires a second PR (scope check per the RFC task), so the vector FILE
  ships here — `tests/fixtures/bundle_contract/vectors.json` (+ its
  regeneration script): matching-legacy, matching-v1, mismatched,
  missing-binding, cross-schema-comparison-refused; digests computed by
  the SHARED renquant-common fingerprint implementation; member
  serialization pinned in-file. Phase 3 moves this file to
  renquant-common so both sides test the same vectors.
- **Tests (+31, all green; full suite: no new failures)**:
  `tests/test_bundle_contract.py` — vector verdicts + true-digest checks;
  runtime equivalence (public verdict == the real
  `_assert_calibrator_matches_scorer` fed by `GlobalPanelCalibration.load`
  and the `PanelScorer.load` metadata steps, per case, plus flag-off
  version-gap equivalence); is-identity binding pins; subprocess
  import-lightness; verdict falsy semantics vs the seam rule; end-to-end
  publish/reject through the REAL phase-1 `BundleStore` with
  `pair_validator=validate_pair`; nine fail-closed edge cases.

## Verification

- Full suite: 1846 passed, 8 skipped; the only failures are the 3
  pre-existing environment-dependent ones (D6 replay pin-platform ×2,
  real-xgboost artifact contract), identical before and after this
  change on the same interpreter. [VERIFIED]
- Live production pair (read-only): `validate_pair` on the deployed
  `artifacts/prod/panel-ltr.alpha158_fund.json` +
  `panel-rank-calibration.json` returns `ok=True, matched_schema=legacy`,
  and the runtime matcher on the same pair does not raise — the two
  surfaces agree on the real serving pair. [VERIFIED]

## Not in this PR (phase 3)

Wiring `BundleStore(pair_validator=validate_pair)` in production writer
tools; promoting `vectors.json` to renquant-common; pinning the manifest
`bindings` block content against member content; the §4 integration proof
that an umbrella-local publication attempt is rejected or has no API.

## RFC ambiguities noted (reported, not improvised)

1. §2.5 does not say how the `accept_legacy_stamps` migration-window flag
   (strategy-config-owned policy) reaches a store-side validator that has
   no strategy config. v1 exposes it as a keyword argument defaulting to
   the runtime's window default (`True`), so default-policy publication
   acceptance can never be looser than serve-time acceptance; phase 3
   should decide who resolves the config at the writer call sites.
2. Scorer kinds the runtime routes to non-default loaders
   (`panel_transformer`/`panel_lgbm`/`panel_linear`) have loader-specific
   metadata semantics this contract version does not pin; contract v1
   fails closed on them (`scorer_kind_unsupported`). Schema v1's member
   set only names the XGB LTR panel artifact, so this is latent.
3. The §2.5 "contract fixture in renquant-common" placement: shipped here
   under `tests/fixtures/bundle_contract/` per the phase-2 scope check;
   the renquant-common move is the phase-3 binding step.
