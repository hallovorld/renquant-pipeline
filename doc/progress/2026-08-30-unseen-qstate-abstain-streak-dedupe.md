# 2026-08-30 — Unseen Q-state ABSTAINS (never 0.0 → positive ER); model_sell streak dedup key persisted

**Bottom line:** two pipeline-kernel defects found by a read-only forensic
pass on the pinned checkout `afb73626` (orchestrator, 2026-08-28). Both fixed
here, pure code, no config / artifact / live-tree change. (1) A per-ticker
Q-learning model returned `raw_score = 0.0` for an unseen state or NaN
features; the isotonic `er_y(0.0)` is POSITIVE on live artifacts and the
rotation horizon scales it ×12 → **3 of 11 live buys 2026-08-18..28 fired on
`raw_score == 0.0`** (CRWD 08-18, PANW 08-19, APH 08-21); on 08-28, 19/87
candidates had raw 0.0, 14 with ER > 0. The model now ABSTAINS: `raw`,
`rank`, `expected_return` are `None`, the candidate is dropped with
`er_abstain_unseen_state`, a held name gets no strike. (2) The once-per-day
dedup key of the model_sell streak, `HoldingState.last_streak_inc_date`, was
never persisted (0 refs in `live_state_v2.py`) → the streak incremented in
BOTH runs of 2026-08-25 (f184d281, bbd3a0f9) and hit 3 at 06:30 08-26 → exit
after two sessions. `LiveStateV2.HoldingV2.last_streak_inc_date` (wire key
`last_streak_inc_dates`) now round-trips it. **The dedup is only LIVE once
the umbrella runner bridges the new key** (repo boundary — see
"What this PR does NOT do"). [VERIFIED — evidence below]

## (1) Unseen Q-state scored as 0.0 → positive expected return

### Mechanism (pre-fix, `kernel/models.py`)

* `predict_qlearning` (`:151-154` pinned): NaN feature → `return 0.0`
  (Issue-38 answer, 2026-05-04). Any resolved state whose Q-row was never
  updated in training also yields `Q(buy) − Q(sell) = 0.0 − 0.0 = 0.0`.
  A live artifact (`XLI-qtable.json`, read-only) has 300,000 rows × 3
  actions; **298,730 (99.6%) are all-zero** — an all-zero row IS the
  unseen-state signal (the tabular learner ships no visit counts).
* `expected_return_from_calibration` (`:113-115`): `np.interp(0.0, er_x,
  er_y)`. `er_y(0.0)` on live artifacts is not 0: MPWR `+0.0034` per 5d
  (XLI `−0.0001`). With `er_lookahead = 5` and
  `rotation.target_horizon_days = 60` the factor is `60/5 = 12`.
* `score_artifact` then reported `signal = "hold"`, but prod runs
  `bypass_ticker_gate: true`, so the tournament signal is advisory and
  the (positive) ER flows into ranking / sizing / rotation.
* The Q-state encodes a `holdings` bucket (`:161-167`), so the candidate
  cell (`holdings=0`, `task_candidates.py:349`) and the held cell
  (`holdings=1`, `task_sell.py:157`) of the same name on the same day are
  different rows (CRWD cand `+0.1187` vs held `−0.2651`, 08-28). Unchanged
  here — but it means the held cell can be unseen while the candidate cell
  is visited, and vice versa; both now abstain independently.

### Fix

* `kernel/models.py`
  * `predict_qlearning` → `float | None`. `None` on: NaN/missing feature,
    state index outside the table, **all-zero Q-row** (unseen).
  * `expected_return_from_calibration(raw: float | None) → float | None`:
    `None` on `None`/non-finite raw — the calibrator is never evaluated on
    an absent score (the previous non-finite branch produced `base = 0.0`
    and then scaled it).
  * `ScoreResult` gains `abstain_reason` (+ `abstained` property);
    `abstain_result()` is the one shape: `raw/rank/ER = None`,
    `signal = "abstain"`. `score_artifact` returns it for a `None`
    Q-prediction BEFORE `calibrate_score` / ER are called.
  * `horizon_extrapolation_report` / `warn_horizon_extrapolation`: pure
    report + ONE WARNING per run when `er_lookahead < target_horizon_days/2`
    (live: `5 < 30`, factor `x12.0`). Item (d): the ×(horizon/lookahead)
    extrapolation itself is NOT changed — separate design decision.
* `kernel/pipeline/task_candidates.py` `ScoreBuyTask`: on abstain →
  `_raw_score/_rank_score/_expected_return/_horizon = None`,
  `model_action = "abstain"`, `blocked_by = "er_abstain_unseen_state"`,
  `return False` — before the `bypass_ticker_gate` branch (bypass waives a
  SIGNAL the model gave; an abstain is the absence of one). The candidate
  is therefore neither a buy nor a rotation buy-leg (both draw from
  `ctx.ranked`), and the blocked reason lands in `_blocked_by_ticker` for
  the decision trace / mass-balance. This is the same "no forecast is
  `None`, never 0.0" contract as the panel-only placeholder (pipeline#302);
  unlike that placeholder nothing downstream stamps a forecast later
  (`global_calibration.enabled: false` in prod), so the drop is here.
  `AssembleCandidateTask` renders `raw=none rank=none` instead of
  formatting `None` with `:.3f`.
* `kernel/pipeline/task_sell.py` `ScoreModelTask`: on abstain →
  `holding.rank_score/expected_return/horizon = None`,
  `model_action = "abstain"`, reason logged. `ModelProtectionExitTask`
  already treats a `None` μ as `mu_unavailable` (no breach counted) — it
  falls back to the panel μ when one exists, which is a real number.
* `kernel/exits.py` `check_model_sell`: `model_action == "abstain"` moves
  the streak neither up nor down (like a non-trading day) and never fires.
  A strike needs a real number; so does a reset.
* `kernel/pipeline/pp_inference.py`: Phase 2b summary line now reads
  `... %d candidates from %d tickers abstain_count=%d`; run counter
  `er_abstain_unseen_state`; `warn_horizon_extrapolation` once per run.
* `kernel/persistence.py`: `model_action` column comment lists `'abstain'`
  (TEXT column, no constraint; `decision_trace` copies the snapshot value).

Scope: **qlearning only.** `predict_classification` / the xgboost NaN
branch still return `0.0` on NaN features (same ER trap in principle;
prod per-ticker models are qlearning). Follow-up, not widened here.

### Pre-fix vs fixed (same probe script, same fixture, both worktrees) [VERIFIED]

```
origin/main afb73626                          this branch
predict_qlearning(unseen) = 0.0               = None
score_artifact(unseen): raw=0.0 rank=0.5      raw=None rank=None
                        er=0.24 signal=hold   er=None signal=abstain
```
(`er=0.24` = `er_y(0.0)=+0.02` per 5d × 12 at the 60d horizon.)

## (2) model_sell streak counted twice on one date

### Mechanism

`exits.check_model_sell` (`:804-806`) dedups with
`state.last_streak_inc_date != today`, but the field lived only on the
in-memory `HoldingState`. `LiveStateV2.HoldingV2` persisted `sell_streak`
and `protection_breaches`, not the date; the umbrella runner restores
`sell_streak` from `sell_streaks` (runner.py:336/615) and nothing else.
Two runs on 2026-08-25 → 2 → 3 → `model_sell streak=3` at 06:30 08-26.

### Fix

* `kernel/live_state_v2.py`: `HoldingV2.last_streak_inc_date: Optional[str]`
  (ISO date, default `None` = never incremented), v1-flat key
  `last_streak_inc_dates` via `_HOLDING_V1_KEYS` (one mapping line, per the
  module's own contract); `streak_inc_date_from_wire` /
  `streak_inc_date_to_wire` helpers for the runner bridge (malformed → `None`
  with a warning, never a raise on a hand-edited file).
* `kernel/exits.py`: comment at the increment site names the persistence
  dependency and the 08-25 incident.

### What this PR does NOT do (repo boundary)

The umbrella runner (`backtesting/renquant_104/adapters/runner.py`) reads the
raw wire (`state.get("sell_streaks")`, `:336`) and builds `HoldingState`
(`:615`) / writes back (`:1824`, `:2015`, `:2098`) itself — it does not go
through `LiveStateV2`. Until it also reads/writes `last_streak_inc_dates`
(and restores `HoldingState.last_streak_inc_date` from it), the second run
of a session still re-increments. The probe above, simulating that runner
(restore `sell_streak` only), still prints `streak=3 should_exit=True` on
BOTH trees. The umbrella change is a 4-line bridge: an orchestrator
follow-up PR against `RenQuant`, after this pin advances. **Merged ≠
deployed** twice over here: pin advance + runner bridge.

## Tests (new: 20 → 2794 passed / 7 skipped; baseline 2774 / 7) [VERIFIED]

`tests/test_unseen_qstate_abstain.py` (16):
* the trap documented: `er(0.0)=+0.02`, `×12 = 0.24` at horizon 60;
* unseen state → `None`; NaN feature → `None`; visited state unchanged
  (`+0.4` flat / `−0.5` held); holdings bucket selects a different cell;
* ER map on `None`/NaN returns `None` without touching `np.interp`
  (monkeypatched to raise); `score_artifact` abstains with
  `calibrate_score` / ER spies **never called**, and calls both for a
  visited state;
* `ScoreBuyTask` drops the abstain with `er_abstain_unseen_state`, all
  four scores `None`, under `bypass_ticker_gate` False AND True; visited
  candidate not dropped; assembly None-safe;
* held: `ScoreModelTask` → `model_action="abstain"`, holding ER/rank/horizon
  `None`; `check_model_sell("abstain")` leaves streak 2 at 2 and a
  streak-3 state does not fire; `model_protection.evaluate(None)` →
  `ACTION_HOLD`, breaches unchanged, `mu_unavailable`; the visited held
  cell still scores `sell` and strikes as before;
* `horizon_extrapolation_report` flags 5 vs 60 (factor 12), not 5 vs 10;
  `warn_horizon_extrapolation` logs exactly one WARNING containing `x12.0`.

`tests/test_model_sell_streak_persisted.py` (4):
* two runs same date through `LiveStateV2` wire → streak +1 once; next date
  → +1 → fires at 3 with the date stamped;
* guard-the-guard: restoring WITHOUT the date reproduces the 08-25 double
  count (`streak=3`, exit);
* field round-trips; absent in a v1 file → `None`, no quarantine; `None`
  omitted from the wire collection (v1 readers iterate it);
* wire helpers (ISO / datetime / garbage / None).

`tests/test_live_state_v2.py`: errata-D matrix extended — golden parse
asserts the field defaults to `None`, rollback wire carries an (empty)
`last_streak_inc_dates`, the 200-case round-trip generator draws the field.

Suite: `make test` with `PYTHON=RenQuant/.venv/bin/python` and the sibling
`*_SRC` paths (the CI command) from the worktree: **2794 passed, 7 skipped,
0 failed**; CI's momentum serving-boundary step
(`tests/test_momentum_residual_shadow_handler.py`): 40 passed.

## Deploy consequence

Nothing changes on the live book until the pipeline pin advances
(`ops/launchd_manifest.json` / `.subrepo_runtime` per the orchestrator's
deploy SOP). After the pin: abstaining names disappear from the candidate
list with `er_abstain_unseen_state` (expect a double-digit `abstain_count`
on the Phase 2b line — 19/87 on 08-28), held names with an unseen held-cell
stop accruing model_sell / model_protection strikes on those days, and one
`ER_HORIZON_EXTRAPOLATION ... x12.0` WARNING appears per run. The streak
dedup across runs additionally needs the runner bridge above.
