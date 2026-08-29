# 2026-08-28 — P-CORR-FRESHNESS: staleness alarm for the served correlation artifact

**Bottom line.** A new SOFT preflight rail, `P-CORR-FRESHNESS`, alarms when the
served correlation artifact's `as_of_date` is older than
`regime.correlation_artifact_max_age_sessions` NYSE sessions (default 30). It
never blocks a run. Against the measured orch#1065 stamp (`2026-05-22`) it
fires with `age=67 NYSE sessions > 30` `[VERIFIED — tests/test_preflight_corr_freshness.py]`.
The config-path decision (which file the strategy should serve) stays with
hallovorld/renquant-orchestrator#1065 — this PR is the alarm, not the path change.

## The measured incident (orch#1065)

- Served artifact `artifacts/prod/watchlist-correlation.json` (config
  `regime.correlation_artifact`) carried `as_of_date=2026-05-22` — 95 days
  stale at the time of measurement — while a fresh artifact sat one directory
  up (`artifacts/watchlist-correlation.json`, where the training writer
  `CorrelationJob` in `kernel/pipeline/pp_training.py` emits it).
- Measured cost at the 0.70 guard threshold: 80 dead blocks + 108 invisible
  conflicts. `[MEASURED in orch#1065; not re-derived here]`
- Nothing alarmed: `P-CORR-METADATA` proves the artifact is *stamped* (the
  leakage contract) and deliberately does not judge the stamp's age.

## Contract (frozen)

`src/renquant_pipeline/kernel/preflight_pipeline/tasks/correlation_freshness.py`

| Aspect | Rule |
|---|---|
| Path | Same resolution as the correlation guard (`ComputeFullSigmaTask._load_corr_from_artifact`): key `regime.correlation_artifact`, default `prod/watchlist-correlation.json`, absolute passthrough, else `<strategy_dir>/artifacts/<rel>` — via the shared `_correlation_artifact_path` helper in `kernel/preflight.py`. |
| Missing file | Defer to `P-CORR-METADATA`: soft `ok=True`, message says "deferred". No duplicated existence failure. |
| Age | NYSE sessions strictly after `as_of_date` through today, via `renquant_common.market_calendar.sessions_between`. If the primitive or its `pandas_market_calendars` backend is unavailable → weekday count, and the message says "WEEKDAYS". |
| Bound | `regime.correlation_artifact_max_age_sessions`, default 30. Malformed / negative / bool → default, with a "malformed … using default 30" note in message + `details["bound_note"]`. `0` is legal. |
| Severity | ALWAYS `soft`. `age > bound` → `ok=False` naming as_of_date, age, bound, path and the fix pointer ("regenerate to the served path or point the config at the maintained file; see orch#1065"). `age <= bound` → `ok=True` naming the age. |
| Stamp absent / unparseable / file unreadable | soft `ok=False` "freshness UNVERIFIED" — absence must not read as fresh. `data_window_end` is accepted when `as_of_date` is absent (the v2 writer stamps both identically); the field used is in `details["stamp_field"]`. |
| Sell-only | Unchanged: soft either way (no `_soft_for_sell_only` routing — nothing to downgrade). |

Registered in `_IdentityJob` right after `CorrelationMetadataTask`
(`preflight_pipeline/pipeline.py`) and in `_LEGACY_CHECK_ORDER` right after
`P-CORR-METADATA` (`kernel/preflight.py`), so `run_preflight` returns it in
that position. Exported from `preflight_pipeline/__init__.py` and
`preflight_pipeline/tasks/__init__.py`.

Note on the task brief: the brief named `_resolve_artifact_path(strategy_dir, rel)`
as the guard's resolver. Read against the code, the guard
(`kernel/portfolio_qp/tasks.py::_load_corr_from_artifact`) does NOT use it — it
resolves `strategy_dir / "artifacts" / rel` directly, and
`_correlation_artifact_path` is the exact preflight mirror of that. The rail
uses the mirror so it checks the file the guard actually reads.

## Test evidence

Runner: `uv run --no-project --python 3.10 --with pytest,xgboost,pandas,numpy,scipy,scikit-learn,pyarrow,pydantic --with-editable ../renquant-common … --with-editable . python -m pytest -q`

| Scope | Clean `origin/main` (e872440) | This branch |
|---|---|---|
| `tests/test_preflight_corr_freshness.py` | n/a | 26 passed, 0 skipped |
| `tests/test_preflight*.py` (incl. new file) | — | 70 passed, 3 skipped (pre-existing skips) |
| Full suite | 2673 passed, 8 skipped, 0 failed | 2699 passed, 8 skipped, 0 failed |

Delta = +26, exactly the new file. No pre-existing failures on the clean base
(`tests/test_wf_fail_override.py` date fix of 08-25 holds). `[VERIFIED — runs of 2026-08-28 22:5x PDT]`

Scenarios covered: fresh → ok naming age; stamped today → 0; age == bound
passes (strict `>`); incident stamp `2026-05-22` → soft not-ok, age 67 NYSE
sessions (holidays 06-19, 07-03 skipped — `pandas_market_calendars` present
in this env and in CI, which installs `renquant-common`), message names
as_of/age/bound/path/orch#1065; legacy v1 (no stamp), garbage string, numeric
stamp, unreadable JSON → UNVERIFIED not-ok; `data_window_end` fallback; missing
file defers (also under `run_mode="full"`); relative + absolute config paths;
bound relax / tighten / zero / 6 malformed forms → default with note; calendar
backend failure → weekday fallback declared in message (age 70); registration
order; through `run_preflight` in `full` and `sell-only` modes the rail is
soft not-ok and absent from the hard-failure slate.

CI coverage of the new file: `.github/workflows/ci.yml` runs `make test` →
`python -m pytest -q` with `testpaths = ["tests"]` (pyproject), so the file is
collected without being named. `[VERIFIED — read ci.yml:57, Makefile:19, pyproject.toml:64]`

## Updated tests

`tests/test_preflight_pipeline_freshness_contract.py`: result count 22 → 23;
asserts `P-CORR-FRESHNESS` sits immediately after `P-CORR-METADATA`.

## Out of scope / follow-ups

- Which path `regime.correlation_artifact` should serve, and whether the
  training writer should emit to `prod/` — strategy-config decision, orch#1065.
- This rail is soft by contract. If the operator wants it to gate buys after
  the path is fixed, that is a separate severity decision (would route through
  `_soft_for_sell_only` like `P-FUND-FRESHNESS`).
