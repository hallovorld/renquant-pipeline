# P-MODEL-STALENESS now covers the xgb primary (was silently skipped)

2026-06-27. Severity: latent gap (no live impact today — see caveat). Closes a
freshness-gate hole found while auditing "why didn't the freshness step work".

## The gap (verified)
`P-MODEL-STALENESS` is the rail that warns when the active scorer outlives its
retrain cadence / training-cutoff decay curve. But it began with:

```python
if str(panel_cfg.get("kind", "xgb")) != "hf_patchtst":
    return PreflightCheck(..., ok=True, "... skip (extend when XGB stamps trained_date)")
```

The live strategy-104 primary is **xgb** (`panel_scoring.kind = "xgb"`,
`artifact_path = artifacts/prod/panel-ltr.alpha158_fund.json`), so the check
**skipped the model actually driving trades** — its age was never gated. The rail
only ever evaluated hf_patchtst, which is currently the shadow.

## Caveat (honest scope)
This is a latent gap, not today's cause of anything: the live xgb primary is in
fact fresh (`trained_date 2026-05-18`, weekly retrains). xgb just doesn't stamp
`effective_train_cutoff_date`, so the decay-curve rail can't be measured for it —
which the old code used as the reason to skip entirely. The fix surfaces that
provenance gap instead of hiding it.

## Fix
- Read the active model's dates by **kind** instead of skipping non-hf_patchtst:
  hf_patchtst → sequence sidecar (unchanged); xgb/panel_ltr_xgboost →
  `trained_date` / `effective_train_cutoff_date` from the artifact JSON itself.
- `trained_date` present, cutoff absent (the xgb case): still evaluate the
  retrain-age rail; report the missing cutoff as a SURFACED provenance gap, not a
  pass. Stays **SOFT** (warn-only) — the WF gate remains the promotion authority,
  and this cannot fail-closed / brick a live run.
- `tests/test_preflight_staleness.py`: replaced the obsolete `xgb_kind_skips`
  test with 4 that pin the new coverage (retrain rail read, breach warns, fully
  fresh+cutoff passes, missing trained_date soft-fails). 10 pass.

## Follow-up (not in this PR)
xgb artifacts should stamp `effective_train_cutoff_date` so the decay-curve rail
is measurable for the primary (currently only the retrain-age rail applies). And
the broader freshness audit (training datasets `transformer_v4` @2026-02-10 /
fundamental @2026-03-24; stale sector-ETF reference artifacts dropped silently)
needs its own tracking — those feed retraining/research, separate from this gate.
