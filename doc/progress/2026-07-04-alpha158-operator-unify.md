# alpha158 serve module → shared-operator shim, anti-skew enforced (campaign B8)

Date: 2026-07-04. Fixes this repo's audit finding §6.3
(`doc/audit/2026-07-03-design-compliance-audit.md`, top P1): the serve-side
`kernel/panel_pipeline/alpha158_features.py` docstring claimed "both build
script and this module import the same low-level functions" while actually
hand-mirroring every operator — unenforced train/serve parity on the LIVE XGB
primary path (`job_panel_scoring.py` `ApplyScoresTask` → `compute_alpha158_at`).
Campaign item B8 (orchestrator `doc/design/2026-07-04-compliance-fix-campaign.md`).

## What changed

- `alpha158_features.py` is now a pure re-export shim of
  `renquant_base_data.alpha158_ops` — the ONE shared train/serve operator
  module (home: base-data, which owns the training panel builder the prod
  model's panel came from; pipeline already declares
  `renquant-base-data>=0.1.0`). Public API unchanged
  (`compute_alpha158_at`, `compute_alpha158_frame`, `alpha158_feature_names`).
  No silent local fallback: if base-data is missing the import fails loudly.
- The false docstring is replaced by the now-TRUE claim + pointers to the
  enforcement tests and the divergence registry.
- NEW `tests/test_alpha158_antiskew.py`:
  - serve ops ARE the shared objects (identity), and the TRAIN builder's ops
    ARE those same objects — train ops == serve ops by construction;
  - AST guard: the serve module must contain zero local `def`s;
  - frame==at lockstep pinned (the pre-B8 docstring cited
    `tests/test_feature_cache.py`, which did not exist — phantom enforcement
    claim, same disease as audit §3.5/6.6).

## Protection-contract proof (real prod OHLCV, read-only)

1600 rows (40 tickers x 40 dates from the prod panel universe): old serve vs
unified serve **max|delta| = 0.0 exactly** for both entry points; old train vs
unified train 0.0 exactly (incl. full ~97k-row builder frames). Pipeline suite
1291→1297 passed, 0 regressions; base-data 238→244.

## Findings reported (NOT changed — pre-existing live behavior)

- **RANK5-60 material train/serve skew**: train=average-rank on ties,
  serve=max-rank; 1-2.8% of real rows diverge, max|delta|=0.2 (RANK5). Live
  the entire time the XGB primary has served. Fix = model-lifecycle decision
  (convention + retrain + gate), campaign follow-up.
- CORD -1-shift (<=1.6e-11) and scalar-vs-vector accumulation (<=7.1e-10,
  identical to the pre-existing cache-hit-vs-miss difference) — fp-grade,
  documented in `KNOWN_TRAIN_SERVE_DIVERGENCES`.

## Deploy path

Depends on the base-data PR (module home) merging first — this repo's CI
checks out base-data@main. Zero live byte change by proof. Path-to-live for
the enforced invariant: base-data + pipeline pins advance, umbrella kernel
mirror sync per campaign Group C (pipeline = kernel authority); until the
mirror sync, the live tree keeps running its current (byte-identical) copy.
