# Universe staleness gate keys on the binding DATA CUTOFF (fail-closed), not trained_date

2026-07-01. Severity: correctness gap in the LIVE universe-admission gate. Closes
the #210 root cause where a fresh `trained_date` over a stale data cutoff wrongly
passes admission. PR only — no deploy.

## The gap (verified)
`FilterStalenessTask` in `kernel/pipeline/job_universe.py` keyed age on the
per-ticker `_metadata.trained_date` and dropped non-held tickers older than
`model_staleness_days` (60). But `trained_date` is the *run time*, not a data
axis: a retrain run today over a stale cutoff stamps a fresh `trained_date` while
being just as blind (freshness-governance design §2, #210). Ground truth: every
one of the 142 live watchlist artifacts carries `live_train_end` (the binding
data cutoff) distinct from `trained_date` — e.g. UPS `trained_date=2026-04-28`
vs `live_train_end=2026-04-20`.

## Fix
- **Key on the binding DATA CUTOFF.** Age = `today − cutoff`, where the cutoff is
  the first present, parseable field in `DATA_CUTOFF_FIELDS`
  (`effective_selection_cutoff_date` → `effective_train_cutoff_date` →
  `data_cutoff_date` → `live_train_end` → `cutoff_date`). This mirrors the
  orchestrator `model_freshness_monitor.DATA_CUTOFF_FIELDS` precedence (#213) so
  the gate and the monitor agree on which axis binds. `trained_date` is
  deliberately NOT in the list. Overridable via
  `config.model_staleness_cutoff_fields`.
- **Fail-closed for offensive (non-held) buys** (Codex review, #213): a missing /
  unparseable cutoff DROPS as `data_cutoff_missing` — never admitted via a
  `trained_date` fallback. A cutoff LATER than today is look-ahead and DROPS as
  `data_cutoff_future`. An in-range-but-old cutoff keeps the existing
  `stale_<age>d_limit_<days>` reason and threshold.
- **Held exemption unchanged.** A currently-held name is still admitted even with
  a missing / stale / future cutoff (sell path stays armed; mirrors
  `FilterUniverseFloorTask`). Each held admit logs which axis was missing/stale.
- No change to the held exemption mechanism, the floor evaluators, or any other
  `job_universe` task.

## Scope / safety
- All 142 current watchlist artifacts carry a parseable `live_train_end`, so
  fail-closed drops **no** live name today purely for a missing cutoff — a ticker
  is dropped only when its data cutoff is genuinely >60d old (the intended #210
  behaviour that stops the stale-data no-buys) or in the future.
- The one artifact lacking `live_train_end` (XLV, a defensive ETF) is NOT in the
  watchlist and `LoadArtifactsTask` only loads watchlist tickers, so it never
  reaches this gate.

## Tests
- New `tests/test_job_universe_staleness.py` (14 cases): non-held fresh →
  admitted; non-held stale / missing / unparseable / future → dropped with the
  right reason; fresh `trained_date` does NOT rescue a stale/missing cutoff; held
  missing / stale / future → still admitted; panel-style field precedence over
  `live_train_end`; configurable field override; disabled at `staleness_days<=0`;
  `age==limit` boundary admitted. 14 pass (38 with the adjacent import + preflight
  staleness suites).

## Follow-up (not in this PR)
Lowering `model_staleness_days` 60 → 28 and the best-of-recent fallback stay
DEFERRED behind the §5 shadow experiment (design §6); this PR only corrects the
freshness *key*, not the threshold.
