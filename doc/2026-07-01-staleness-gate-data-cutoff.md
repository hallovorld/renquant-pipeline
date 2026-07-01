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
- **Key on the binding DATA CUTOFF, and evaluate EVERY required axis** (Codex
  review round 2, #213/#423). Data freshness is not a single axis: the as-of used
  to SELECT a model (`effective_selection_cutoff_date`) and the cutoff of the data
  it TRAINED on (`effective_train_cutoff_date` → `data_cutoff_date` →
  `live_train_end` → `cutoff_date`) are SEPARATE required facts. The gate no
  longer picks "the first present field" as the single binding axis — that was the
  bug: a fresh `effective_selection_cutoff_date` could hide a stale
  `effective_train_cutoff_date`. It now checks BOTH the `training_data` axis
  (mandatory) and the `selection` axis (evaluated when present); a fresh axis
  never masks a stale one. Within an axis the aliases keep the monitor's field
  precedence, so gate ⇄ `model_freshness_monitor` still read the same field for
  the same fact. `trained_date` is deliberately in NO axis.
- **Fail-closed for offensive (non-held) buys, naming the exact field.** A
  missing / unparseable / future / stale value on ANY required axis DROPS the
  ticker with the offending field in the reason: `data_cutoff_missing`,
  `data_cutoff_unparseable:<field>`, `data_cutoff_future:<field>`,
  `stale_<age>d_limit_<days>:<field>`. Never admitted via a `trained_date`
  fallback.
- **As-of / session date threaded through `UniverseContext`.** `date.today()` is
  no longer embedded; `uctx.as_of_date` (a `date`, or a `datetime` normalized to
  its `.date()`) drives freshness so replay / as-of runs are deterministic and
  never wall-clock- or session-boundary-dependent. `None` → `date.today()` on the
  live path (unchanged default).
- **Operator override cannot erase mandatory provenance.**
  `config.model_staleness_cutoff_fields` now only APPENDS extra training-data
  aliases (lowest precedence); a built-in field, when present, always binds ahead
  of it. An operator list can no longer silently hide a mandatory axis.
- **Held exemption preserved, refined for untrusted provenance** (Codex review
  point 3). An aging-but-VALID cutoff (known past date, merely `stale`) still
  admits the model so the `model_sell_streak` exit stays armed. An UNTRUSTED
  cutoff (missing / unparseable / FUTURE — cannot prove the artifact is not
  look-ahead) does NOT admit the scorer wholesale: the name is removed from
  `loaded_models` and recorded in the new `uctx.fallback_exit` contract so a
  downstream consumer applies a model-INDEPENDENT (position / risk) exit. The
  position stays exitable without trusting a bad model and is never hard-rejected.
- No change to the floor evaluators or any other `job_universe` task.

## Scope / safety
- `LoadUniverseJob` / `UniverseContext` / `FilterStalenessTask` are not yet wired
  into any adapter (grep: only tests import them), so this is a correctness change
  to a pre-wiring module. `fallback_exit` defines the contract the future
  consumer reads alongside `loaded_models`.
- All 142 current watchlist artifacts carry a parseable `live_train_end`, so
  fail-closed drops **no** live name today purely for a missing cutoff — a ticker
  is dropped only when a data cutoff is genuinely >60d old (the intended #210
  behaviour that stops the stale-data no-buys) or in the future.
- The one artifact lacking `live_train_end` (XLV, a defensive ETF) is NOT in the
  watchlist and `LoadArtifactsTask` only loads watchlist tickers, so it never
  reaches this gate.

## Tests
- `tests/test_job_universe_staleness.py` (25 cases): non-held fresh → admitted;
  non-held stale / missing / unparseable / future → dropped with the field-named
  reason; fresh `trained_date` does NOT rescue a stale/missing cutoff; **fresh
  selection does NOT mask stale training and the inverse** (#213/#423 core);
  future selection with fresh training fails closed; held stale → still admitted,
  held missing / unparseable / future → routed to `fallback_exit` (not admitted,
  not rejected), future-axis beats stale-axis for fallback; as-of replay
  (admission flips on `as_of_date` not wall clock; cutoff-after-as-of is future
  even if past today; tz-aware datetime normalized to session date); training
  alias precedence; axis fields match the monitor; override cannot erase mandatory
  provenance + override adds a working custom alias; disabled at
  `staleness_days<=0`; `age==limit` boundary admitted. 25 pass (35 with the
  adjacent preflight-staleness suite).

## Follow-up (not in this PR)
Lowering `model_staleness_days` 60 → 28 and the best-of-recent fallback stay
DEFERRED behind the §5 shadow experiment (design §6); this PR only corrects the
freshness *key*, not the threshold.
