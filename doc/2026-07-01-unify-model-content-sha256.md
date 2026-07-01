# Unify calibrator/scorer `model_content_sha256` — fixes recurring fingerprint-mismatch fail-closed

**Date:** 2026-07-01 · **Author:** Claude · **Status:** PR open, fix landed here + renquant-common + renquant-model

## The bug

`panel_scorer.py::model_content_sha256` (this repo, the RUNTIME-AUTHORITATIVE
check consumed by `_assert_calibrator_matches_scorer` in `job_panel_scoring.py`)
and `fit_calibrator_alpha158_fund.py::model_content_sha256` (renquant-model, the
CALIBRATOR FIT-TIME stamp) were independently hand-copied implementations that
hash DIFFERENT field sets for the same logical concept:

- This repo: DENYLIST style — excludes a curated `_MUTABLE_ARTIFACT_KEYS` set,
  keeps everything else.
- renquant-model: ALLOWLIST style — an explicit 11-field dict that INCLUDES
  `label_col` and lacks several fields the denylist keeps (e.g. `kind`).

A calibrator fit by one could never match the runtime check by another, **by
construction** — not transient drift. This fail-closed monthly whenever
`monthly_calibrator_refresh.sh` re-fit the calibrator: 2026-05-27, 2026-06-22,
2026-07-01.

## The fix

Extracted the canonical implementation (this repo's denylist logic — the
runtime-authoritative one, whose docstring's design intent already says
"invariant to metadata edits") into `renquant_common.model_fingerprint`
(renquant-common `0.8.1`). Both renquant-pipeline and renquant-model already
depend on renquant-common, so this is not new cross-subrepo coupling.

- `panel_scorer.py` now imports `model_content_sha256` / `artifact_sha256` /
  `stamp_artifact_metadata` / `model_content_sha256_from_path` /
  `_MUTABLE_ARTIFACT_KEYS` / `_PREDICTIVE_CONTENT_HINTS` from
  `renquant_common.model_fingerprint` and re-exports them under their original
  names for back-compat (other modules in this repo — `hf_patchtst_scorer.py`,
  `walk_forward/loader.py`, tests — import these names from `panel_scorer`).
- `pyproject.toml` pin bumped to `renquant-common>=0.8.1,<0.9` — a structural
  requirement, not just a range widen: below 0.8.1 the shared module doesn't
  exist.
- New regression test `tests/test_model_content_sha256_shared.py` pins that
  `panel_scorer.model_content_sha256 IS renquant_common.model_fingerprint.model_content_sha256`
  (same object, not a value-equal copy) — so nobody can silently re-fork a
  local copy without a test catching it.

## Also checked (not touched)

- `renquant-backtesting`'s `wf_gate/stamp_walkforward_fingerprints.py` and
  `walk_forward/loader.py` already `import model_content_sha256` from this
  repo's `panel_scorer` (not a copy) — they inherit the fix automatically,
  no change needed there.
- `renquant_model_patchtst/fit_calibrator.py`'s `_artifact_fingerprint` does
  NOT independently recompute the hash — it only reads already-stamped
  fingerprint fields with a raw-file-hash fallback. Not a 4th divergent
  algorithm; left as-is.
- Umbrella-local `backtesting/renquant_104/kernel/panel_pipeline/model_fingerprint.py`
  hashes only `booster_raw_json` bytes (a 3rd, different algorithm) but has
  no importers beyond its own test — confirmed dead code. Not fixed here
  (umbrella tree is out of scope / do-not-write per hard boundary); left for
  a follow-up umbrella-only cleanup PR.

## Dependency order

This PR depends on `renquant-common` PR (adds `renquant_common.model_fingerprint`,
bumps to 0.8.1) landing first — CI here checks out renquant-common's `main`
branch, so this PR's CI won't go green until that one merges.
