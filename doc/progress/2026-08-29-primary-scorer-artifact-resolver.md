# 2026-08-29 — PRIMARY scorer + global calibrator resolve artifacts through the ONE authority (orch#1066 option a')

**Bottom line.** The PRIMARY panel-scorer loader and the global calibrator
joined a relative `artifact_path` onto `_strategy_dir` ONLY, while blend
components (and every preflight check) resolve through
`kernel/artifact_resolver.py` with a repo-root fallback. The same ref string
therefore meant two different files depending on which loader read it. This
PR routes both loaders through `artifact_resolver.locate_artifact` — identical
precedence to the blend components (absolute → strategy_dir → repo_root), no
other behaviour change. Production (`strategy_config.json`, a blend whose refs
all live in the strategy bundle) resolves to byte-identical paths; the shadow
config's `hf_patchtst` PRIMARY leg becomes loadable again. Shadow-lane change
only; nothing is armed or deployed by this PR.

## The asymmetry (what was wrong)

| loader | resolution rule at origin/main `76ab129` | file:line |
|---|---|---|
| PRIMARY scorer (`LoadScorerTask._resolve_artifact_path`) | `Path(strategy_dir) / ref` only | `src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py:915-928` |
| blend component-0 anchor (`_blend_component0_path`) | `Path(strategy_dir) / ref` only | `job_panel_scoring.py:943-949` |
| global calibrator (`LoadGlobalCalibrationTask._resolve`) | `Path(strategy_dir) / ref` only | `job_panel_scoring.py:3183-3184` |
| blend components (`blend_scorer._resolve_component_path`) | `artifact_resolver.resolve_artifact` → strategy_dir, then repo_root | `blend_scorer.py:324-332`, `artifact_resolver.py:51-58` |
| every preflight check (`preflight._resolve_artifact_path`) | `artifact_resolver.locate_artifact` → strategy_dir, then repo_root | `preflight.py:65-74` |

`artifact_resolver.py:8-13` documents that this exact class of bug (primary
and shadow resolving one ref against different roots) already killed a shadow
artifact for a week (pipeline#114); the module was created to be the ONE
authority, but the PRIMARY loader never adopted it.

**Consequence, measured on the live umbrella tree (read-only)** — the pinned
shadow config `renquant-strategy-104@d3c8026 configs/strategy_config.shadow.json`
is `kind=hf_patchtst` with
`artifact_path=artifacts/patchtst_shadow/pt07_strict_trainfit_embargo60_20260522/seed_44/hf_patchtst_all_seed44_model.pt`
[VERIFIED — json read of the pinned config]. That file:

- `RenQuant/artifacts/patchtst_shadow/.../hf_patchtst_all_seed44_model.pt` —
  **EXISTS**, 301,047 bytes, mtime 2026-05-22 14:04 [VERIFIED — `stat` on the
  umbrella tree, read-only];
- `RenQuant/backtesting/renquant_104/artifacts/patchtst_shadow/...` —
  **MISSING** [VERIFIED — same probe].

So the primary loader hands the strategy-dir path to `HFPatchTSTPanelScorer.load`,
which raises → `LoadScorerTask` logs `failed to load hf_patchtst artifact …` →
`_fail_closed_panel_scoring(ctx, "panel_scorer_load_failed")`
(`job_panel_scoring.py:1136-1141`) → every buy candidate cleared → the
read-only e2e verify (`scripts/check_readonly_e2e.sh`, which auto-selects the
shadow config) is permanently red; RenQuant#614 now classifies that as exit 3.
orch#1066 reported the artifact as "deleted" — it was looked for under the
strategy dir only; the repo-root copy the blend components would have found is
intact. Option (a') = fix the resolver, not the config.

## The fix

`job_panel_scoring.py` — one module-level helper, three call sites:

```python
def _locate_config_artifact(strategy_dir, ref) -> Path:
    from renquant_pipeline.kernel.artifact_resolver import locate_artifact
    return locate_artifact(
        ref, strategy_dir=Path(str(strategy_dir)) if strategy_dir else Path("."),
    )
```

- `LoadScorerTask._resolve_artifact_path` → `_locate_config_artifact(...)`
- `LoadScorerTask._blend_component0_path` → `_locate_config_artifact(...)`
- `LoadGlobalCalibrationTask._resolve` → `_locate_config_artifact(...)`
  (covers the pooled calibrator, `calibrator_per_regime`, and the
  `regime_conditional` pattern — all three go through that closure)

Why `locate_artifact` rather than `resolve_artifact`: both share the single
`_candidates` precedence (`artifact_resolver.py:51-58`), so the ORDER is
identical to the blend components by construction. `locate_artifact`
(`artifact_resolver.py:85-98`) never raises and returns the strategy_dir
candidate on a miss — exactly the path the pre-fix code produced — so every
downstream failure path is untouched: the kind loader raises the same
exception on the same path, `LoadScorerTask` fails closed with the same
`panel_scorer_load_failed`, the specialists branch keeps
`panel_specialist_load_failed`, the pooled calibrator keeps
`calibrator_load_failed`, `calibrator_per_regime` keeps its own
`FileNotFoundError` message, and `regime_conditional` keeps its
pooled-fallback-on-miss. `resolve_artifact` would have raised at the anchor
step (before the per-branch handlers), re-routed the specialists reason code,
and `.resolve()`d the stamped path string. No `_strategy_dir` → `Path(".")`
(the blend components' own convention, `blend_scorer.py:330`), which reduces to
the bare ref — the pre-fix behaviour.

The blend components' own resolution (`blend_scorer.py`) is not touched.
NGBoost (`job_panel_scoring.py:3702-3706`) and the regime-router sub-scorers
(`model_registry.py:80-87`) still use the strategy_dir-only join; both are
disabled/unused in every pinned config and are out of scope here (noted for a
follow-up, not silently changed).

## Why production is unaffected

The pinned production config `renquant-strategy-104@d3c8026 configs/strategy_config.json`
is `kind=blend` with components `artifacts/prod/panel-ltr.alpha158_fund.json`
(classic leg) and `artifacts/momentum/momentum_artifact_ledger.jsonl`
(`momentum_residual`); `global_calibration.enabled=false`, `ngboost.enabled=false`,
no `specialists` [VERIFIED — json read of the pinned config]. On the live tree
both component refs EXIST under `backtesting/renquant_104/` and do NOT exist at
the repo root [VERIFIED — `test -f` on both roots, read-only]. Under the new
rule the strategy_dir candidate is tried first and wins, so:

- the blend components' path list is produced by untouched code
  (`blend_scorer._resolve_component_path`) and is asserted equal to the
  strategy_dir list (`TestProductionBlendUnchanged::test_blend_component_path_list_is_the_strategy_dir_list`);
- the component-0 anchor used by the consistency gate and the trace stamp is
  string-identical to main's `Path(strategy_dir) / ref`
  (`test_blend_component0_anchor_unchanged`);
- every other ref in the production config resolves to the same path as
  before (`test_every_production_ref_resolves_as_before`).

The assertions are written against the resolver's output, not by re-running
the models.

## Test evidence

New file `tests/test_primary_scorer_artifact_resolver.py` (20 tests). It is
collected automatically by `make test` (`python -m pytest -q` from the repo
root — the CI "Test" step); no workflow edit is needed.

| invariant | test |
|---|---|
| strategy_dir copy present → strategy_dir path, byte-identical to the legacy join | `TestPrimaryResolution::test_strategy_dir_copy_wins_and_is_byte_identical_to_legacy`, `TestLoadSite::test_fresh_load_receives_strategy_dir_path_when_present` |
| only repo_root copy → repo_root path == the blend components' answer (the fix) | `test_repo_root_fallback_is_the_fix_and_matches_blend_components`, `TestLoadSite::test_fresh_load_receives_repo_root_path`, `test_preloaded_scorer_branch_stamps_repo_root_path` |
| neither → same path as before, real `xgb` loader raises the same class + text, contract fails closed with `panel_scorer_load_failed`, 2 candidates cleared, blocked map + counter set | `TestLoadSite::test_missing_everywhere_fails_closed_with_same_error_and_reason` |
| absolute ref / no `_strategy_dir` / no `artifact_path` / scorer-metadata ref — unchanged | `test_absolute_ref_untouched`, `test_no_strategy_dir_reduces_to_bare_ref`, `test_no_artifact_path_still_none`, `test_scorer_metadata_ref_takes_precedence_and_resolves_same_way` |
| production blend shape → path list + anchor unchanged vs main | `TestProductionBlendUnchanged` (4 tests) |
| global calibration: strategy_dir first, repo_root fallback, miss keeps `calibrator_load_failed` + logged path, `calibrator_per_regime` precedence + unchanged miss message | `TestGlobalCalibrationResolution` (5 tests) |

Suite, run the CI way (`uv run --no-project --python 3.10 --with pytest,xgboost,…
--with-editable <siblings> python -m pytest -q`):

- origin/main `76ab129` (throwaway worktree): **2744 passed, 8 skipped, 0 failed**
  [VERIFIED — this session, `baseline.log`]. (The 2702/8/0 figure quoted in the
  task brief predates the merges since; the measured baseline is 2744.)
- this branch: **2764 passed, 8 skipped, 0 failed** [VERIFIED — this session,
  `fix-full3.log`] = baseline + 20 new, no regressions. (Two intermediate runs
  on this branch failed only the twin-pairs pin checks — 8, then 1 — until the
  re-pin + exception + test-pin update above were in place; nothing else moved.)
- targeted: `tests/test_primary_scorer_artifact_resolver.py tests/test_artifact_resolver.py
  tests/test_blend_scorer.py tests/test_panel_scoring_contract.py
  tests/test_active_scorer_attribution.py` → 113 passed [VERIFIED — this session].

## Twin-pairs re-pin (kernel-only, justified)

`LoadScorerTask` is a twin pair (`twin_pairs.json`): the DOCUMENTED public
export resolves to `src/renquant_pipeline/panel_scoring.py:119-135`, a
different implementation that validates an artifact-manifest contract on
`ctx` and never resolves a filesystem path — there is no `artifact_path` /
`_strategy_dir` call site there to receive this change (same structural
reason as the pipeline#258 lineage entry it supersedes). The kernel digest
moves `ef0f9156…` → `6e22f089…`, `public_sha256` unchanged; recorded as a
`_repin_2026_08_29` note in `twin_pairs.json` and a replacement
`LoadScorerTask` entry in `twin_repin_exceptions.json` bound to both exact
digest tuples. `tools/twin_pairs.py` → `twin-pairs OK — 56 public exports
pinned, 19 with a kernel twin`; the PR step
`--diff-against <base pins> --base-exceptions <base exceptions>` →
`no one-sided re-pins against the given baseline`. The exception list is
itself pinned in `tests/test_twin_pairs_one_sided_repin.py`
(`test_the_committed_exception_file_is_well_formed_and_every_entry_is_bound`),
so that expected tuple moves in the same diff, as that test's docstring
requires. `tests/test_twin_pairs*.py` (3 files) + the new file → 83 passed
[VERIFIED — this session]. `LoadGlobalCalibrationTask` is not a public export
and has no twin.

## Kernel-parity note (umbrella vendored copy)

The umbrella vendors the pipeline kernel under `backtesting/renquant_104/kernel/`.
`RenQuant/scripts/check_kernel_parity.py` `KNOWN_DRIFT_ALLOWLIST` (read-only)
already lists BOTH touched files — `"artifact_resolver.py"` (line 41) and
`"panel_pipeline/job_panel_scoring.py"` (line 66) — and both umbrella copies
already differ from origin/main today [VERIFIED — `cmp` against the worktree].
They are in the allowed-drift set, not the identical set, so the pin-bump PR
does NOT have to sync the umbrella copy for the parity check to stay green.
Note for the deployer: the daily run executes the pinned pipeline package
(`.subrepo_runtime`), not the vendored copy, so the fix is live once the pin
advances; the vendored copy remains a stale mirror either way.

## What this changes for the shadow lane (and only there)

Once this lands and the pipeline pin advances, `strategy_config.shadow.json`'s
`hf_patchtst` PRIMARY resolves to the repo-root artifact (source `repo_root`),
its global calibrator to the strategy-dir copy (present there), and the
read-only e2e verify can produce a decision again — RenQuant#614's exit-3
preflight ("missing artifact") will pass. Whether that 2026-05-22 PatchTST
artifact is worth serving in shadow is a separate question (memory:
"PatchTST scores intrinsically negative", served primary 625d stale) and is
NOT decided here; this PR only makes the config mean what the blend
components already thought it meant. The production 13:55 lane loads the same
files from the same paths as before.

## Not done / follow-ups

- NGBoost and regime-router sub-scorer refs still join onto strategy_dir only
  (both disabled in every pinned config).
- orch#1066 should be updated: the artifact was never deleted; it lives at the
  repo root and only the PRIMARY loader could not see it.
- Pin advance + umbrella deploy are separate, operator-gated steps.
