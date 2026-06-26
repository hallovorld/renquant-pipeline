# Shadow scoring: skip a non-history shadow fed a degenerate panel matrix

2026-06-26. Severity: low (shadow-only, **zero live-trading impact**). Fixes a
false-alarm `model_contract` HARD FAIL that read as a "model error" in ops.

## Symptom
The daily shadow e2e run (`strategy_config.shadow.json`, hf_patchtst primary)
logged two ERRORs from the legacy `xgb_alpha158_fund_previous_primary` shadow
comparison:

```
[model_contract] PanelScorer.input HARD FAIL: pct_zero_var_cols=100.0% (>50%)
[model_contract] PanelScorer.score HARD FAIL: collapsed prediction - x-sec std=3.7e-09, n_unique=1
```

The live primary (hf_patchtst) scored normally (`std≈0.034`); only the
non-history xgb shadow collapsed.

## Root cause
`ApplyShadowScoringTask` feeds a NON-history shadow scorer `ctx._panel_matrix`
(`shadow_scoring.py`). When the PRIMARY scorer is **history-based** (hf_patchtst),
the xgb-rows block in `job_panel_scoring` that stamps a valid per-ticker
cross-section into `ctx._panel_matrix` never runs (that block is the BUG #6,
2026-05-09 fix path) — so the matrix presented to a non-history shadow has a
constant value for every ticker. `X[fc].fillna(0)` is then cross-sectionally
flat, the xgb produces an identical score for all names, and `model_contract`
HARD-FAILs (`abs(col_std) < 1e-12` over >50% of cols). The existing guard only
skipped on *missing* columns, not on *present-but-constant* ones.

It surfaced on 2026-06-26 (not before) only because the shadow e2e run finally
completed end-to-end that day — prior runs short-circuited in 2–4s on stale state
and never reached this legacy shadow.

## Fix
`shadow_scoring.py`:
- New `_is_degenerate_cross_section(sub)` — True when >50% of columns have
  `abs(std) < 1e-12` (≥2 rows required). Threshold mirrors `model_contract`'s
  input HARD-FAIL exactly, so the guard fires precisely when the contract would.
- In the non-history shadow path, skip with a WARNING (not a HARD FAIL) when the
  input is degenerate — a meaningless comparison, not a model fault.

`tests/test_shadow_scoring_degenerate_matrix_guard.py`:
- Unit tests for the helper (all-constant / >50% / <=50% / varied / single-row).
- Wiring tests: a degenerate `ctx._panel_matrix` skips the shadow scorer (never
  calls `.score()`), while a varied matrix still scores it (guard does not
  over-fire). 3 pass; adjacent collapse/telemetry tests unaffected (6 pass).

## Not changed
The live trader is unaffected — it never consumed this shadow. The legacy
`xgb_alpha158_fund` shadow comparison is simply skipped on history-primary runs
instead of emitting a false-alarm collapse. (Whether to retire that shadow entry
from `strategy_config.shadow.json` is a separate config decision.)
