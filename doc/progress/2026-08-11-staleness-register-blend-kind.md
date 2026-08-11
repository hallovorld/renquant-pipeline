# 2026-08-11 — Register `blend` in P-MODEL-STALENESS: a z-composite is only as fresh as its OLDEST leg

STATUS:   FIXED (2026-08-11). Staleness suite (blend + unknown-kind + preflight
          + freshness-contract) **37 passed**
          `[VERIFIED — .venv/bin/python -m pytest -q tests/test_staleness_blend.py tests/test_staleness_unknown_kind.py tests/test_preflight_staleness.py tests/test_preflight_pipeline_freshness_contract.py]`.
          Full suite **2601 passed / 9 skipped / 2 pre-existing unrelated
          failures** in `tests/test_replay_d6_conventions.py`
          `[VERIFIED — .venv/bin/python -m pytest -q]`; those same 2 failures
          reproduce identically on the unmodified pre-change head (baseline
          **2590 passed / 9 skipped / 2 failed**, `_t_stat: null` numpy-
          platform diff — this PR touches neither that file nor its inputs)
          `[VERIFIED — full suite on `main` @ 08f526ee before any edit]`.
          The 11-test delta = 12 new blend tests − 1 removed `blend`
          parametrize case in the unknown-kind module. SOFT preserved; no
          other branch touched (staleness.py diff = **182 insertions, 0
          deletions**).

WHAT:     Adds a `kind == "blend"` branch to `ModelStalenessTask`
          (`src/renquant_pipeline/kernel/preflight_pipeline/tasks/staleness.py`).
          A blend's freshness is bound to the **stalest** (oldest cutoff / max
          age) of its `ranking.panel_scoring.components` legs. Each leg is
          resolved by REUSING the per-kind reads the existing branches already
          use — a direct-artifact leg (absent `kind`/`"panel"` default, or
          `"xgb"`/`"panel_ltr_xgboost"`) is read from the artifact JSON exactly
          like the xgb branch; an `"hf_patchtst"` leg is read via
          `_load_sequence_sidecar` exactly like the patchtst branch. Per-leg
          ages land in `details["legs"]`; `details["binding_retrain_leg"]` /
          `["binding_cutoff_leg"]` name which leg binds.

WHY/DIR:  Prod strategy-104 serves `panel_scoring.kind = "blend"` (a z-blend:
          a `panel-ltr.alpha158_fund` production-scorer leg + a
          `momentum_residual` leg)
          `[VERIFIED — python -c "json.load(...strategy_config.json)['ranking']['panel_scoring']['kind']" -> 'blend'; components len 2, comp0 has NO kind key (panel default), comp1 kind='momentum_residual']`.
          `blend` was not a registered kind, so it took the unrecognised-kind
          else branch: the LIVE prod scorer's decay/retrain rail was never
          established and the daily model_freshness monitor escalated
          `[unknown] prod-panel: binding data cutoff unknown (fail-closed)`.
          A real observability gap — we could not monitor whether the traded
          scorer was going stale. The check is SOFT (does not block trading);
          it stays SOFT.

## The rule (stalest-leg-binds), quoted from the diff

The binding leg on each axis is the max-age (oldest) leg:

```python
retrain_leg = max(legs, key=lambda leg: leg["retrain_age_days"])
...
cutoff_leg = max(legs, key=lambda leg: leg["cutoff_age_days"])
```

and the pass message reports it as the binding constraint:

```
blend fresh: binding leg component[{i}] ({kind}) retrained {N}d ago,
oldest cutoff {M}d old across {len(legs)} legs
```

This mirrors the canonical authority `blend_scorer.BlendPanelScorer`, whose
own metadata already sets `effective_train_cutoff_date` = `min(cutoffs)` (the
OLDER leg) and `None` when either leg is unstamped — the same conservative
staleness contract, now enforced by the freshness rail.

## Fail-closed provenance discipline (unchanged from the rail's existing behaviour)

A leg whose kind this rail does not register (e.g. `momentum_residual`, whose
append-only ledger axis is a distinct registration — deliberately NOT
reimplemented here), whose artifact is unreadable, or whose `trained_date`
cannot be established, is a SURFACED gap NAMING the leg — never a false
"fresh" pass:

```python
leg["gap"] = (f"component[{index}] kind={comp_kind!r} is not a "
              f"staleness-readable leg kind — this rail cannot establish "
              f"its freshness axis (register the kind)")
```

An unestablished leg BINDS the blend (a blend cannot be fresher than a leg
whose age is unknown), so the whole check is a SOFT non-pass even when the
other leg is perfectly fresh — with the readable leg's age still reported so
the finding is actionable. Test: `test_momentum_residual_leg_is_soft_nonpass_naming_it`
(leg0 fresh, leg1 `momentum_residual` → `ok=False`, `severity="soft"`, message
names `component[1]` + `momentum_residual` + the readable `component[0]
retrain_age=20d`).

## Real-config smoke (read-only, verifies the gap is closed)

Feeding the live served `strategy-104/configs/strategy_config.json` through the
check now yields a SOFT, structured, per-leg finding — `component[0] (panel)`
resolved to the JSON read and `component[1] (momentum_residual)` named as the
registration gap — instead of the opaque "kind not registered"
`[VERIFIED — ran ModelStalenessTask().check on the live config; severity=soft, ok=False, message enumerates both legs by index/kind]`.
(In this static strategy-104 checkout component[0]'s artifact is served from
the umbrella tree, not committed to the strategy repo, so it reads
"unreadable" here — a fixture artifact of the checkout, not the branch; the
runtime resolver finds it via the repo-root fallback.)

## Preserved byte-for-byte

The `hf_patchtst`, `xgb`/`panel_ltr_xgboost`, and unrecognised-non-blend
(`else`) branches and the common date-evaluation tail are untouched
`[VERIFIED — git diff on staleness.py shows only additions: the `if kind == "blend"` dispatch + two new helper methods, 0 deletions]`.
`tests/test_staleness_unknown_kind.py` swaps its `blend` representative to
`ensemble` (still genuinely unregistered) so the inverted-default else branch
stays under test; `xgb`/`hf_patchtst`/`ensemble`/`patchtst`/`""`/`None`
behaviour is unchanged.

## Scope / follow-up

Reading the `momentum_residual` leg's ledger freshness (via the existing
`load_momentum_residual_scorer` chain/golden-reproduction loader) is a
distinct kind-registration and is left as a surfaced follow-up, not silently
passed — exactly the "register the kind to make this actionable" discipline
the top-level else branch already keeps for the primary.

## Boundaries honoured

No live-config / umbrella / order writes; work done in a fresh clone off
`main` (not the live tree, not `.subrepo_runtime`). No `--admin`, no
self-merge. Codex approval remains the mechanical gate.
