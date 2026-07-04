# M6 stage-2 step-1: schema-version-dispatched fingerprint verification

Design: renquant-orchestrator
`doc/design/2026-07-03-m6-stage2-fingerprint-migration.md` §3 step 1.
Successor to #159/#160 (the reverted v1 cutover): this lands the migration
mechanism #160 said was missing. Step 0 (legacy pre-stamp of the live
inventory) executed 2026-07-03 — 47/47 artifacts carry legacy stamps, so the
stamped-value routes are authoritative everywhere.

## The dispatch contract (one module, both fail-closed checks)

`kernel/panel_pipeline/fingerprint_dispatch.py` is the ONE place the pipeline
decides how a scorer/calibrator identity pair is compared:

- Every stamp carries (or lacks) `fingerprint_schema_version`; an artifact is
  verified under the ONE semantics its stamp declares. A versionless stamp IS
  the legacy (0.8.1) declaration.
- v1/v1: exact digest equality; the scorer's v1 stamp is `verify()`'d against
  its payload where the payload is available (`PanelScorer.load`, the WF fold
  read) — fail-closed. No prefix acceptance on the v1 route.
- legacy/legacy: the pre-existing shim equality path, byte-for-byte
  (multi-key list + historical 12-char prefixes). Dies with the flag at
  step 4.
- cross-schema: never a match (one acceptable hash per artifact — a v1
  mismatch can never hide behind a passing legacy hash).
- Flag: `ranking.panel_scoring.fingerprint.accept_legacy_stamps` (default
  `true` = the migration window; policy owned by strategy config). `false` ⇒
  only the v1 route exists; a versionless stamp fails closed with the
  "re-stamp under v1" remedy.
- Unstamped artifacts fall back to the pre-existing recompute behavior, with
  the venv-coupled bare `model_content_sha256` replaced by the EXPLICIT pair
  (legacy shim + v1 recomputes) — the identity no longer silently changes
  semantics when the venv's renquant-common version changes (the #160
  problem). Production is never unstamped post step-0 (census-enforced).

## Enforcement points (design §3 step 1, verbatim)

- `job_panel_scoring.py::_assert_calibrator_matches_scorer` (strict daily buy
  path — the 2026-05-27/06-22/07-01 incident site).
- `walk_forward/loader.py::_assert_calibrator_matches_entry` +
  `_scorer_claim_for_entry` (replaces the old fail-soft bare-name recompute
  at loader.py:158; a v1-stamped fold with a corrupt stamp now fails closed
  at read).
- `PanelScorer.load` stamps BOTH identities into in-memory metadata (legacy
  via the 0.9.1 shim + v1 recompute under telemetry-only keys) and logs the
  `fingerprint-dispatch load:`/`verify:` divergence-telemetry lines — the
  step-3 census criterion (e) source.

## Behavior guarantees

- Flag at default + legacy-stamped population (today's live state): verdicts
  identical to pre-dispatch behavior; the full suite + dispatch fixtures pin
  this (`tests/test_fingerprint_version_dispatch.py`, all four §6 step-1
  acceptance cases).
- `tests/test_model_content_sha256_shared.py` rewritten per the design:
  is-identity pins on the v1 API re-exports + frozen legacy AND v1
  test-vectors (the #21 fixture ground truth) that must keep passing until
  step 5 removes the shims.

## Dependencies / sequencing

- Requires renquant-common >= 0.9.2 for step-2 dual-stamped artifacts
  (`model_content_fingerprint_legacy_081` / `restamp_provenance` classified
  OPERATIONAL — hash-preserving, schema version stays 1). The code itself
  runs against 0.9.1 (the fields only appear after the step-2 re-stamp run).
- strategy-104 introduces the flag explicitly in a separate config PR
  (default `true`); absent key == `true` here, so merge order is free.
- Step 5 (renquant-common 0.10) removes the shim re-exports + the legacy
  route + the legacy-vector test pins.
