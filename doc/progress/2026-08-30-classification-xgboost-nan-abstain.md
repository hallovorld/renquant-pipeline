# 2026-08-30 — Classification / xgboost per-ticker models ABSTAIN on NaN inputs (same trap as #303)

**Bottom line:** pipeline#303 made the per-ticker Q-learning model abstain
(`raw/rank/ER = None`, signal `abstain`) instead of scoring `0.0` on NaN
features / an unseen Q-row — because the isotonic ER calibrator maps
`raw = 0.0` to a POSITIVE expected return (×12 at the 60d rotation horizon).
Its scope note left the two other learned model types on the pre-fix path:
`predict_classification` (VLO = Classification, 15 trees) and the xgboost
branch of `score_artifact` (NVDA) still mapped a NaN / missing feature to
`raw = 0.0` → isotonic → ER > 0. This PR closes that: **a missing / None /
NaN required feature now abstains for classification, xgboost AND qlearning,
with `abstain_reason = "nan_features"`** (blocked_by
`er_abstain_nan_features`); a never-visited Q-row stays `unseen_state`.
**Real votes are untouched** — a classification vote that lands exactly on
the calibrator's neutral value is a vote, not an unseen state, and is
returned byte-identical to the pre-fix value. Stacked on #303; pure code,
no config / artifact / live-tree change. [VERIFIED — tests below]

## Mechanism (pre-fix, `kernel/models.py` at #303 head 621ab231)

* `predict_classification` (`:145-151`): `any(isnan(feat_vals)) → return 0.0`.
* `score_artifact` xgboost branch (`:400-408`): `any(isnan(feat_vals)) →
  raw = 0.0`; otherwise `P(buy) − P(sell)`.
* Both then flowed into `calibrate_score(0.0)` and
  `expected_return_from_calibration(0.0, …, horizon_days=60)` — the exact
  path #303 documents for qlearning (`er_y(0.0) > 0` on live artifacts,
  scaled `60/5 = 12`), with `bypass_ticker_gate: true` in prod so the
  advisory `hold` signal did not stop the ER from reaching ranking / sizing
  / rotation.
* `predict_xgboost` itself routes NaN by `default_left` (audit M-4) — but
  `score_artifact` never reached it on a NaN row; it short-circuited to
  `0.0` first. That helper is unchanged for direct callers.

## Fix

* `kernel/models.py`
  * `missing_features(artifact, row) → list[str]`: required
    `feature_columns` that are absent / `None` / NaN / non-coercible.
  * `score_artifact`: for `classification`, `qlearning`, `xgboost` —
    `missing_features(...)` non-empty → `abstain_result(ABSTAIN_NAN_FEATURES)`
    **before dispatch**; the trees / booster / Q-table / calibrator / ER map
    are never evaluated (tests spy on all of them). `manual` is rule-based
    and skips NaN rules by its own contract (`predict_manual`) — not a
    learned model, left alone.
  * `predict_classification → float | None`: `None` on missing features
    (defensive second layer; `score_artifact` also handles the `None`).
    The vote arithmetic is byte-for-byte the pre-fix expression.
  * xgboost branch: the `raw = 0.0` NaN short-circuit is deleted; NaN can
    no longer reach it.
  * Reason vocabulary: `ABSTAIN_NAN_FEATURES = "nan_features"`,
    `REASON_ER_ABSTAIN_NAN_FEATURES = "er_abstain_nan_features"`,
    `REASON_ER_ABSTAIN_PREFIX`, `abstain_block_reason(reason)`,
    `is_abstain_block_reason(blocked_by)`, `abstain_breakdown(values)`.
    `REASON_ER_ABSTAIN_UNSEEN_STATE` is unchanged (`er_abstain_unseen_state`).
  * qlearning label change: a NaN row through `score_artifact` now reports
    `nan_features` (no state can be resolved = absent input), not
    `unseen_state` as under #303 alone. The unseen / out-of-table Q-row
    still reports `unseen_state`; `predict_qlearning`'s own NaN → `None`
    guard remains for direct callers. #303's tests pass unchanged.
* `kernel/pipeline/task_candidates.py` `ScoreBuyTask`: `blocked_by =
  abstain_block_reason(sr.abstain_reason)` — `er_abstain_nan_features` or
  `er_abstain_unseen_state`. Everything else (drop before the
  `bypass_ticker_gate` branch, all four scores `None`, `model_action =
  "abstain"`) is #303's path, now reached by every model type.
* `kernel/pipeline/task_sell.py` `ScoreModelTask`: no change needed —
  `sr.abstained` already clears the holding's ER / rank / horizon and logs
  the reason; `check_model_sell("abstain")` and
  `model_protection.evaluate(None)` count no strike (#303).
* `kernel/pipeline/pp_inference.py`: Phase 2b line now reads
  `abstain_count=%d (er_abstain_nan_features=%d er_abstain_unseen_state=%d)`;
  one run counter per reason (`ctx.counters["er_abstain_<reason>"]`),
  both keys always present.

## Not unseen: a real vote at the neutral value

A Classification forest whose trees split +1 / −1 on a row produces a mean
vote of exactly `0.0`; the calibrator maps that to ER > 0 too. That is NOT
the trap — the model saw the input and voted. Only absent input abstains.
Pinned by `test_classification_vote_at_neutral_value_is_a_vote_not_an_abstain`:
`raw == 0.0` (bit-identical), `not abstained`, `signal hold`, `rank 0.5`,
`ER 0.24` at horizon 60 — unchanged from the pinned code.

## Tests — `tests/test_classification_xgboost_nan_abstain.py` (29) [VERIFIED]

* the trap documented (`er(0.0) = +0.02`, ×12 = 0.24);
* `missing_features`: NaN / absent column / `None` / non-numeric / both;
* `predict_classification`: NaN / absent / `None` → `None`; four real rows
  (incl. exactly-on-split) `struct.pack('<d')`-identical to the pinned
  expression re-stated verbatim in the test; vote values checked;
* neutral-value vote is a vote (above);
* `score_artifact` classification abstains with `calibrate_score`, ER map
  AND `_traverse_tree` spies never called; real vote: `raw 0.9`, `ER 0.672`;
* `score_artifact` xgboost abstains (NaN / absent / `None`) with the
  calibrator, ER map AND `predict_xgboost` spies never called; real row
  `raw = σ(1) − σ(−1) ≈ 0.4621 → buy`, other side of the split → sell;
* qlearning: NaN / absent → `nan_features`; unseen row → `unseen_state`;
  visited row not abstained;
* reason helpers + `abstain_breakdown` (stable two-key shape);
* `ScoreBuyTask` (classification AND xgboost): NaN row dropped under
  `bypass_ticker_gate: true` with `er_abstain_nan_features`, all four
  scores `None`, `model_action abstain`; real row admitted as `buy` with
  horizon 60;
* `ScoreModelTask` (classification AND xgboost): NaN row → `abstain`,
  holding ER / rank / horizon `None`, `check_model_sell("abstain")` leaves
  streak 2 at 2 and does not fire; real row → `buy` with finite ER.

Suite: `make test` with `PYTHON=RenQuant/.venv/bin/python` and the sibling
`*_SRC` paths (the CI command) from the worktree:
**2823 passed, 7 skipped, 0 failed** (+29 = the new file) (#303 head: 2794 passed / 7 skipped).

## Deploy consequence

Nothing changes on the live book until the pipeline pin advances past #303
and this PR. After the pin: any per-ticker model (Classification / xgboost /
qlearning) with a NaN or missing feature on the day is dropped from the
candidate list with `er_abstain_nan_features` (visible per reason on the
Phase 2b line and in `ctx.counters`), and a held name in that state accrues
no model_sell / model_protection strike that day. Prod per-ticker models are
predominantly qlearning, so the expected live delta is small (VLO, NVDA
class); the value is closing the trap uniformly rather than per type.
