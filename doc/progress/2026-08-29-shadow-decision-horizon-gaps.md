# 2026-08-29 — Shadow-lane commit fails on `decision_horizon_gaps`: a panel-only placeholder forecast (orch#1082)

**Bottom line:** the shadow config's read-only funnel died at COMMIT because
`ScoreBuyTask` stamps a **`0.0` expected return with a `None` horizon** on
every panel-only candidate (a watchlist ticker with no tournament artifact,
admitted only when `ranking.panel_scoring.candidate_universe = "watchlist"`).
`ApplyGlobalCalibrationTask` is the only thing that replaces that placeholder
— with the horizon — and any candidate dropped before it (`risk_gate_vol`,
`panel_score_missing`, `panel_scorer_load_failed`) carried the pair into
`candidate_scores` / `ticker_daily_state`. The decision-trace validator
counts `expected_return IS NOT NULL AND expected_return_horizon_days IS NULL`
as a gap and the commit raises. Fix (pipeline-owned, pure code): "no
forecast yet" is `None`, not `0.0`. Every reader between candidate assembly
and calibration already treats `None` as absent. Prod is unaffected because
its config has no `candidate_universe = "watchlist"`: it never creates a
model-less candidate, so every candidate's ER comes from `score_artifact`
with the rotation horizon stamped beside it. [VERIFIED — evidence below]

## Symptom

```
RuntimeError: RunnerAdapter.commit: decision trace integrity failed for
run_id=2026-08-29-live-a64257a6: {"decision_horizon_gaps": 5, ... 0 ...}
```
and on 2026-08-25 (`run_id=2026-08-25-live-250372bf`, the
`panel_scorer_load_failed` day): `candidate_horizon_gaps: 15,
decision_horizon_gaps: 20`
(`RenQuant/logs/issue1021_postclose_rerun_2026-08-25.log:276`).

Raised by `renquant_pipeline/kernel/persistence.py:2654`
`validate_decision_trace_integrity` → `decision_trace_integrity_report`;
the two counters are `:2481-2497`:

```sql
-- candidate_scores / ticker_daily_state
(expected_return IS NOT NULL AND expected_return_horizon_days IS NULL)
OR (mu IS NOT NULL AND mu_horizon_days IS NULL)
```

The umbrella adapter only calls it (`backtesting/renquant_104/adapters/runner.py:2346`)
after `record_candidate_scores` (`:2174`) and
`build_ticker_daily_state_rows` + `record_ticker_daily_state` (`:2215-2231`);
every value in those rows is produced by pipeline code.

## Evidence rows (copy of `RenQuant/data/runs.alpaca_shadow.db`, read-only)

`ticker_daily_state`, run `2026-08-29-live-a64257a6` (145 rows, all
`model_type=hf_patchtst`) — the 5 gap rows, `(ticker, blocked_by,
expected_return, er_horizon, mu, mu_horizon, in_universe, in_candidates)`:

```
CRWV  risk_gate_vol        0.0  None  None  None  0  0
INTC  risk_gate_vol        0.0  None  None  None  0  0
RBLX  risk_gate_vol        0.0  None  None  None  0  0
RKLB  risk_gate_vol        0.0  None  None  None  0  0
SPCX  panel_score_missing  0.0  None  None  None  0  0
```

Run `2026-08-25-live-250372bf`: 20 gap rows = the same 5-shape
(`risk_gate_vol`: CRWV, INTC, QCOM, RBLX, RKLB) + 15 `panel_scorer_load_failed`
rows (CAT, COST, CSCO, CVX, FDX, GOOG, LMT, MCD, MO, SO, SPCX, SPOT, TJX,
WMT, XOM), all `expected_return = 0.0`, horizon `NULL`, `in_universe = 0`;
those 15 also sit in `candidate_scores` with the same pair →
`candidate_horizon_gaps = 15`.

Every gap row is `in_universe = 0` (no per-ticker tournament model,
`runner.py:2222 model_keys=set(self._models)`) and `expected_return` is
exactly `0.0`. Risk-gated tickers WITH a tournament model on the same run
(e.g. AMAT: `risk_gate_vol`, ER 0.0758, horizon 60, `in_universe = 1`) are
clean — their ER came from `score_artifact` with the horizon stamped.

Same tickers on the blend path (`2026-08-28-live-3b828643` in the shadow DB,
`2026-08-28-live-83d6e1a8` in `runs.alpaca.db`): `universe:no_artifact` /
`universe:sharpe_*_below_0.5`, `expected_return NULL`, 0 gaps. Every
`2026-08-2*` run in the prod DB: 0 gaps.

## Mechanism (pinned pipeline == `origin/main` a7fb14ef, byte-identical here)

1. Shadow config sets `ranking.panel_scoring.candidate_universe = "watchlist"`
   (+ `bypass_ticker_gate: true`, `enabled: true`); prod's key is absent.
   `_panel_watchlist_candidate_mode` (`kernel/pipeline/pp_inference.py:187-207`,
   twin in `task_candidates.py:12-27`) → `_buy_universe` (`pp_inference.py:236-240`)
   sources candidates from the **watchlist** instead of `ctx.models`.
2. For a ticker with `tc.model is None`, `ScoreBuyTask`
   (`kernel/pipeline/task_candidates.py:320-330`) writes the placeholders
   `model_action="panel_pending"`, `_raw_score=0.0`, `_rank_score=0.0`,
   **`_expected_return=0.0`, `_expected_return_horizon_days=None`**, with
   the comment "PanelScoringJob overwrites these placeholders".
3. The placeholder is copied twice: into `ctx._ticker_score_snapshot`
   (`pp_inference.py:494-506`, keyed by ticker, survives every later drop)
   and into `CandidateResult.expected_return` by `AssembleCandidateTask`
   (`task_candidates.py:421-431`; `selection.py:34` typed `float = 0.0`).
4. Only `ApplyGlobalCalibrationTask` replaces it — and stamps the horizon
   in the same breath (`panel_pipeline/job_panel_scoring.py:3466-3468`,
   holdings `:3520-3526`).
5. Candidates dropped before that point keep the pair:
   `RealizedVolGateTask` (`task_risk_gates.py:96`, wired BEFORE Phase 3),
   `_drop_unscored_panel_candidates` → `panel_score_missing`
   (`job_panel_scoring.py:871-`), `_fail_closed_panel_scoring` →
   `panel_scorer_load_failed` (`:843-868`, which also moves the
   `CandidateResult`s into `_full_candidate_snapshot` → `candidate_scores`).
6. `build_ticker_daily_state_rows` (`kernel/decision_trace.py:334-337`,
   `_score_value` `:195-197`) reads ER from the candidate, else from the
   snapshot → `0.0` with `NULL` horizon → validator gap → commit raises.

## Fix (this PR)

* `kernel/pipeline/task_candidates.py` `ScoreBuyTask`: the panel-pending
  placeholder is `_expected_return = None` (raw/rank stay `0.0`: they are
  not validator-covered, and `rank >= floor` math downstream needs a float).
* `AssembleCandidateTask`: carries `None` into `CandidateResult` and renders
  `er=none` in `detail` instead of formatting `None` with `:+.4f`.
* `kernel/selection.py`: `CandidateResult.expected_return: float | None`
  (default unchanged) with the invariant spelled out.
* Readers audited for `None` (no change needed): `rotation.py:583`,
  `task_joint_actions.py:413/435/471/483/972-976`, `task_rotation.py:675`
  (`float(... or 0.0)`); `job_panel_scoring.py:2541-2543, 3642-3644`,
  `signal_direction.py:140`, `task_buy_quality_gates.py:126-133`,
  `task_selection.py:512`, `governor_sizing.py:181`, `order_attribution.py:109`,
  `decision_ledger.py:367`, `pp_inference.py:502` (`is None` / pass-through).
  The behaviour of every gate is identical to the `0.0` placeholder; only
  what gets persisted changes.
* Existing test `tests/test_panel_watchlist_candidate_universe.py` updated
  (`_expected_return is None`).

### Regression: `tests/test_panel_only_candidate_horizon_trace.py`

Drives the real tasks + real persistence into an in-memory SQLite and
asserts on `decision_trace_integrity_report` — the comparison the live
commit makes:

1. panel-only candidate → `RealizedVolGateTask` drop → 0 gaps; row is
   `(ER NULL, horizon NULL, risk_gate_vol, in_universe 0)`;
2. panel-only candidate → `_fail_closed_panel_scoring("panel_scorer_load_failed")`
   (the 08-25 shape) → 0 gaps on both tables;
3. control: a tournament candidate with `ER 0.031 / horizon 20` dropped the
   same way still persists both (the fix does not blank real forecasts);
4. guard-the-guard: the pre-fix pair (`0.0`, `None`) on both tables IS
   counted (`decision_horizon_gaps == 1`, `candidate_horizon_gaps == 1`,
   `ok False`).

Run against the unfixed source (src stashed in this worktree): tests 1 and 2
fail with exactly the live counters (`decision_horizon_gaps 1`,
`candidate_horizon_gaps 1`). With the fix: 4/4 pass. [VERIFIED]

Note for reviewers: running the new file from another checkout's rootdir
imports THAT checkout's `renquant_pipeline` via the worktree conftest — the
first "pre-fix" attempt passed for that reason; the stash run above is the
real pre-fix measurement.

## Why prod is unaffected (and never was)

`strategy_config.json` has no `ranking.panel_scoring.candidate_universe`
key (`kind: blend`), so `_panel_watchlist_candidate_mode` is False,
`_buy_universe` returns `ctx.models`, and every candidate has a tournament
artifact whose `score_artifact` result is stamped WITH
`rotation.target_horizon_days` (`task_candidates.py:344-347`). Model-less
watchlist names are rejected at universe admission (`universe:*`) with
`expected_return NULL`. Measured: 0 gaps in every `2026-08-2*` run of
`runs.alpaca.db` and in the blend-config shadow runs (08-25/26/28).

## Verify consequence

`scripts/check_readonly_e2e.sh` (umbrella) will progress past COMMIT for
the shadow config once this lands AND the pipeline pin advances; until the
pin advances the pinned assembly still carries the placeholder and the
verify stays red for this reason. No config, flag, artifact, or live-tree
change is part of this fix.

## Suite

`uv run --no-project --python 3.10 --with pytest,... --with-editable <siblings> python -m pytest -q`
from the worktree: **2768 passed / 8 skipped / 0 failed** (= baseline + the 4 new tests) (baseline 2764 passed / 8 skipped / 0 failed).
