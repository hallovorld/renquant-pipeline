# 2026-07-04 — Port the 06-15 PatchTST mis-score fix to the live authority copy (campaign A1)

Compliance fix campaign item **A1** (orchestrator PR #297, finding RQ#444 F-1).

## The finding

The 2026-06-15 HF-PatchTST "silent mis-score" fix existed ONLY in the umbrella
mirror (`RenQuant/backtesting/renquant_104/kernel/panel_pipeline/hf_patchtst_scorer.py`,
commits `6cb2d79` + `1a91680`). The LIVE path runs this repo's copy
(`src/renquant_pipeline/kernel/panel_pipeline/hf_patchtst_scorer.py`), which still
had the pre-fix loader: model constructed with `use_distributional_head` only and
a bare `load_state_dict(state, strict=False)` whose result was swallowed.

Consequence: a checkpoint trained with the OPTIONAL architecture components
(cross-stock attention and/or FiLM regime conditioning) loaded with its
`cross_stock.*` / `film.*` tensors silently dropped as "unexpected", so the
forward pass ran through the channel-independent baseline — **wrong scores and
never an error**. The shadow PatchTST scores DAILY on this copy and would become
the primary scorer on any re-promote.

## The port (verbatim-in-semantics from the umbrella reference)

- `_checkpoint_component_flags`, `_required_state_prefixes`,
  `_summarize_key_roots`, `_fail_loud_on_arch_mismatch` helpers (umbrella
  `1a91680`).
- `load()` reconstructs `HFPatchTSTRanker` with `use_film_regime` /
  `use_cross_stock_attn` read from the checkpoint (umbrella `6cb2d79`).
- `load()` checks the `load_state_dict(strict=False)` result:
  - any **unexpected** tensor → `ValueError` (fail loud — the incident shape);
  - any **missing** tensor under a declared component's prefix
    (`backbone.` / `rank_head.` / `dist_head.` / `film.` / `cross_stock.`)
    → `ValueError` (fail closed — never score through a random layer).
- Log line now reports `film=`/`cross_stock=` flags.

Only adaptation: none needed beyond placement — the helpers are pure Python; the
pipeline copy keeps its own (newer) import machinery for `HFPatchTSTRanker`.

## Regression (incident fixture)

`tests/test_hf_patchtst_scorer_cross_stock.py` rebuilds the incident: a
checkpoint whose cross-stock layer is trained-like (non-identity —
`CrossStockAttentionLayer` is identity-at-init via its alpha gate, so a fresh
layer would mask the bug).

- `test_incident_fixture_old_loader_semantics_mis_score` pins the OLD behavior
  as broken: the verbatim pre-fix load path drops exactly `cross_stock.*` and
  the crippled model's scores diverge from the trained model's.
- Fixed loader: reconstructs the layer and reproduces the trained model's
  scores exactly; baseline checkpoints load clean (no false positive);
  unexpected tensors fail loud; declared-but-missing components fail closed
  (cross_stock and film variants).
- Counter-proof run on pristine `main` (unfixed copy): the 4 loader-behavior
  tests FAIL there — the guard catches the live bug.

## Remaining umbrella↔pipeline deltas for this file (mirror-drift inventory)

After the port, `diff` umbrella vs pipeline leaves only:

1. `stamp_artifact_metadata` import path — lift artifact (package-qualified
   here). Keep.
2. `load()` import machinery — pipeline is AHEAD (canonical
   `renquant_model_patchtst.hf_trainer` import + guarded file-import fallback,
   v3-PR #7); umbrella still file-imports `scripts/patchtst_hf.py`. Keep
   (pipeline is the authority).
3. Umbrella-only #426 r9 block: reads persisted `provenance_schema_version` /
   `recipe_id` / `required_axis_fields` from the checkpoint into metadata
   (shadow-ntfy provenance chain, 2026-07-02). NOT ported — it is a separate,
   newer umbrella feature coupled to the save-side stamping and the
   shadow-admission consumer; porting it alone here would be scope creep on a
   live-behavior single. Disposition: candidate lift for the Group-C drift
   inventory.
4. The port-provenance comment block added here. Cosmetic.

## Suite A/B

Full pipeline suite green on the branch; vs pristine `main` the only differing
file is the new `tests/test_hf_patchtst_scorer_cross_stock.py` (see PR body for
the enumerated A/B).
