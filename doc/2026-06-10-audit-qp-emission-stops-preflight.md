# Deep Bug Audit — QP/Emission + Stops + Preflight + State

Date: 2026-06-10
Branch: `audit/qp-emission-stops-preflight`
Scope: `kernel/portfolio_qp/`, `kernel/preflight_pipeline/`, exit/stop logic
(`kernel/exits.py`), exit wiring (`kernel/pipeline/pp_inference.py`,
`task_sell.py`, `exit_params.py`), execution-state (`task_execution.py`).
Read-only audit — no code changed. Reproductions run with
`/Users/renhao/git/github/RenQuant/.venv/bin/python`; trade evidence from
`RenQuant/data/runs.alpaca.db`.

Out of scope (other auditors): scoring/calibrator internals,
admission/selection/sizing internals.

The four "known bugs" listed in the brief (single-day-loss whipsaw measured,
QP turnover deadlock, Davis-Norman/soft-sell exit blocking, WF-gate bare
imports) are NOT re-reported as new findings except where I found a *distinct*
mechanism still live on `main`. The qp_contracts/sim_ledger bare-import bug
appears already fixed (no such imports remain in `gate.py`).

---

## Severity summary

| Severity | Count |
|----------|-------|
| Blocker  | 1 |
| High     | 5 |
| Medium   | 5 |
| Low      | 3 |
| Not-a-bug (documented) | 2 |

### Top 3 most serious

1. **B-1 (Blocker)** — `apply_stop_loss_anchor_policy` raises `ValueError`
   inside the un-guarded `_make_sell_tctx` list comprehension; one held
   position with an entry-regime missing `stop_loss_pct` aborts sell
   evaluation for *every* holding that bar, blocking all risk exits.
2. **H-1 (High)** — Single-day-loss / trailing / SDL-σ stops are NOT anchored
   to the entry regime (only `stop_loss_pct` is). A BULL_CALM 60-day thesis
   re-labeled to BULL_VOLATILE silently switches to a tight absolute 6% SDL.
3. **H-2 (High)** — `sdl_skip_if_unrealized_above` defaults to 0 (off), so the
   single-day-loss gate whipsaws large winners on a noise gap-down. Live
   evidence: NVTS exited via `single_day_loss` at **+113%** after 8 days held.

---

## BLOCKER

### B-1 — Anchor-policy exception aborts ALL sell evaluations for the bar
- File: `src/renquant_pipeline/kernel/pipeline/exit_params.py:61-63`
  (raises) and `src/renquant_pipeline/kernel/pipeline/pp_inference.py:90-96`
  + `:293` / `:547` (un-guarded call site).
- Mechanism: `apply_stop_loss_anchor_policy(...)` raises `ValueError` when
  `risk.stop_loss_anchor_policy.mode == "max_entry_current"`, the policy
  matches the entry/current regimes, but the **entry-regime** config has no
  `stop_loss_pct`. `_make_sell_tctx` calls it with no try/except, and
  `pp_inference` builds the sell contexts with a bare list comprehension
  `sell_tctxs = [_make_sell_tctx(ctx, t) for t in _sell_universe(ctx)]`. A
  single offending holding throws → the whole comprehension dies → the
  parallel `TickerSellJob` never runs for ANY holding → no stop-loss /
  trailing / SDL / max-hold / model-sell exits emit that bar.
- Trigger conditions (all live in prod-shaped configs): anchor policy enabled
  (the representative config sets `mode=max_entry_current`,
  `entry_regimes=[BULL_CALM]`), AND a held position whose `entry_regime` is a
  regime whose `regime_params` lacks `stop_loss_pct` (a renamed/removed
  regime, or any entry regime without that key). The current-regime branch is
  also gated, but an entry regime label that no longer maps is enough.
- Repro:
  ```
  apply_stop_loss_anchor_policy({'stop_loss_pct':0.05}, config=cfg,
      current_regime='BEAR', entry_regime='BULL_CALM', entry_regime_params={})
  # -> ValueError: stop_loss_anchor_policy requires stop_loss_pct for entry regime BULL_CALM
  ```
- Why blocker: it fails CLOSED on the *exit* path. The mechanism that is
  supposed to keep stops armed across a regime deterioration is the same
  mechanism that, on a config edge, disables every exit in the bar.
- Suggested fix: wrap the anchor-policy call per-ticker in try/except that
  logs + falls back to current-regime stop (its documented "current" mode);
  and/or build `sell_tctxs` with a per-ticker guard so one bad holding can't
  take down the slate. The policy should degrade to current-regime, never
  raise, on missing entry evidence.

---

## HIGH

### H-1 — SDL / trailing / σ-stops are regime-unconditional (only stop_loss anchored)
- File: `src/renquant_pipeline/kernel/pipeline/pp_inference.py:74-96`
  (`_make_sell_tctx` / `_build_exit_params`); anchor only covers
  `stop_loss_pct` (`exit_params.py:56-67`).
- Mechanism: `_build_exit_params` reads `max_single_day_loss_pct`,
  `sdl_n_sigma`, `trailing_stop_*`, `stop_n_sigma` from the **current**
  regime. Only `max_hold_days` is anchored to the entry regime (line 80-82),
  and only `stop_loss_pct` is run through `apply_stop_loss_anchor_policy`.
  A position entered in BULL_CALM on a 60-day thesis (representative config:
  `sdl_n_sigma=3.0`, `max_single_day_loss_pct=0`) that gets re-labeled
  BULL_VOLATILE silently inherits the volatile SDL (`max_single_day_loss_pct
  =0.06`) and tighter trailing — a tight, noise-sensitive stop on a thesis
  that was selected to ride a month-plus horizon.
- Evidence: representative config
  `RenQuant/doc/research/.../strategy_config.sim_kelly_sigma_horizon60.json`
  defines `stop_loss_anchor_policy` for `stop_loss_pct` ONLY; SDL/trailing
  have no anchor. `runs.alpaca.db`: `single_day_loss` exits avg `pnl_pct
  = +0.09` (firing on positions that were *up* ~9%), avg hold 21.7d;
  `stop_loss` avg hold 42.5d on a 60-day thesis.
- Suggested fix: extend the anchor policy (or a sibling) to SDL/trailing/σ
  params, anchoring path-risk stop *width* to the entry thesis the same way
  `max_hold_days` already is.

### H-2 — `single_day_loss` whipsaws large winners (skip-guard defaults OFF)
- File: `src/renquant_pipeline/kernel/exits.py:573-644` (`check_single_day_loss`);
  param default in `pp_inference.py:58`
  (`sdl_skip_if_unrealized_above ... default 0`).
- Mechanism: `sdl_skip_if_unrealized_above` (B2 revival) suppresses SDL when
  the position is a winner by ≥ X%, but defaults to 0 = disabled, and the
  representative BULL_CALM config does not set it. So a 3σ (≈15-16%) gap-down
  on a stock up +100%+ from entry crystallizes a forced exit on pure noise.
- Evidence (`runs.alpaca.db`): NVTS exited via `single_day_loss` at
  `pnl_pct=+1.13` (+113%) after **8 days held**; 44 of 137 `single_day_loss`
  exits closed in profit. Repro confirms a +110% winner with a 16% gap and
  3σ=15% threshold fires with skip off and is suppressed with skip>30%.
- Suggested fix: ship a non-zero `sdl_skip_if_unrealized_above` default (e.g.
  0.20) for momentum regimes, or gate SDL on `unrealized < 0` for long-horizon
  theses. Pairs with H-1.

### H-3 — Proportional-trade shrink + no-trade `min_dw` floor starves new buys
- File: emission path `src/renquant_pipeline/kernel/portfolio_qp/tasks.py`
  — `ApplyProportionalTradeTask` (`:2847-2905`) runs at job step
  `job_qp.py:511`, BEFORE `EmitOrdersFromQPSolutionTask` (`job_qp.py:514`)
  which applies `_passes_no_trade_band` (`tasks.py:2255-2305`, `:3144`).
- Mechanism: `ApplyProportionalTradeTask` rewrites `delta_w = (target -
  current)/N` for ALL names (including fresh buys). The no-trade band then
  applies the *same* `min_dw` floor (prod `qp_min_dw_pct = 0.02`) to the
  shrunk Δw. Under N=20 (the value the `proportional_trade.py` docstring
  recommends for BULL_CALM), a new name needs a **40%** target weight
  (`min_dw × N`) before any first slice clears the band → every realistic new
  buy is skipped as `qp_delta_below_min_dw`, every bar. This is the same
  *class* as the documented turnover deadlock but a distinct, still-live
  mechanism (band vs partial-trade are uncoordinated).
- Repro: `proportional_trade_target(current=[0,0.1], target=[0.04,0.1],
  n_days=20)` → first-slice Δw=0.002; `_passes_no_trade_band(0.002,
  min_dw=0.02)` → `pass=False`.
- Status: LATENT in the representative config (`qp_partial_trade_horizon_days`
  is unset there) but armed the moment an operator follows the module's own
  N=20 guidance. Marked High because the docstring actively recommends it.
- Suggested fix: when partial-trade is active, scale the band/`min_dw` by 1/N
  (the band should gate the *frictionless* Δw, not the per-bar slice), or
  apply the band before the proportional shrink.

### H-4 — No-trade band suppresses QP-desired risk-reducing trims/exits
- File: `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:3144-3161`
  (band gate applied to every ticker, buys AND sells, in `_emit_orders_loop`).
- Mechanism: `_passes_no_trade_band` is applied symmetrically to sells. A QP
  solution that wants to trim a held name by a small amount (e.g. Δw = -1.9%)
  is blocked by the same `min_dw=2%` floor meant to throttle nuisance churn.
  Full closes (target≈0) usually survive because |Δw| is large, but partial
  de-risking trims — and any exit shrunk by H-3's proportional trade — are
  silently suppressed. This is the live mechanism behind the "QP wanted to
  exit ORCL at +1.9% but couldn't" class.
- Repro: `_passes_no_trade_band(-0.019, sig=0.02, min_dw=0.02,
  factor=2.0)` → `pass=False`. ORCL was finally stopped out via the
  path-stop (`stop_loss`, BULL_CALM, 2026-06-10) rather than a QP trim.
- Suggested fix: exempt risk-reducing trims of *held* names from the `min_dw`
  floor (the band exists to suppress discretionary micro-churn, not de-risking),
  or apply the band only to buys / top-ups.

### H-5 — Intra-bar buys cannot use cash freed by later sells (order-dependent starvation)
- File: `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:3107-3271`
  (`_emit_orders_loop`).
- Mechanism: `buy_cash_left` is seeded once (`:3107`) and only credited with
  sell proceeds (`_long_sell_credit`, `:3263-3265`) *after* a sell is emitted
  in loop order. Because tickers are iterated in their natural `_qp_tickers`
  order with no pre-pass, a buy at index i is cash-capped/`cash_exhausted`
  even when a sell at index j>i would free ample cash that same bar. Net bar
  cash supports the trade; emission order does not. Contributes to the
  turnover/no-trade-deadlock symptom set.
- Suggested fix: pre-sum the bar's projected sell credit (or process sells
  before buys) so buy sizing sees post-sell cash.

---

## MEDIUM

### M-1 — `stop_decay` collapses the stop to ZERO (disables it) past 2×decay_days
- File: `src/renquant_pipeline/kernel/exits.py:521-552`; default mismatch in
  `pp_inference.py:61` (`stop_decay_floor` default 0) vs the function default
  0.5 (`exits.py:481`).
- Mechanism: `_build_exit_params` passes `stop_decay_floor` from
  `regime_p.get("stop_decay_floor", 0)` — i.e. **0** when the operator enables
  `stop_decay_days` but omits the floor — while `check_stop_loss`'s own
  default is 0.5. With floor=0, at `held = 2×decay_days` the multiplier hits
  exactly 0, so `abs_thresh = 0`, then `threshold = max(0, sigma_thresh)`; if
  σ-stop is off, `threshold <= 0` → `return _NO_EXIT`. The stop meant to
  *tighten* on long-held bleeders instead **fully disables** itself.
- Repro: held=60 / decay_days=30 / floor=0 → effective stop = 0.0000 →
  `should_exit=False` even at -1%.
- Status: opt-in (`stop_decay_days` default 0), so it only bites when decay is
  enabled with floor left unset.
- Suggested fix: make `_build_exit_params` default `stop_decay_floor` to 0.5
  (match the function), and/or floor the post-decay multiplier above 0 so the
  decayed stop never evaluates to a disabling 0 threshold.

### M-2 — Short detection inconsistent between `check_single_day_loss` and `check_stop_loss`
- File: `src/renquant_pipeline/kernel/exits.py:557` (stop uses
  `total_shares()`/lots) vs `:631` (SDL uses `state.shares`).
- Mechanism: `check_stop_loss` derives short-ness from `total_shares()` (lot
  aware), but `check_single_day_loss` reads only `state.shares`. For a short
  carried solely in `lots` with `state.shares == 0`, the stop fires correctly
  but the SDL computes the loss with the long sign → wrong side, never fires.
- Repro: short via lots only (`shares=0`): `stop_loss` exits, SDL does not.
- Status: Phase-2A short path normally populates `state.shares`, so this is a
  latent consistency bug, not yet observed in long-only prod.
- Suggested fix: route both checks through one `_position_is_short(state)`
  helper using `total_shares()`.

### M-3 — Calibrator health vs flat-region gates can read DIFFERENT artifacts
- File: `src/renquant_pipeline/kernel/preflight_pipeline/tasks/calibrator.py:65-66`
  (`CalibratorHealthTask` uses `cal_cfg.get("artifact_path", default)` ONLY)
  vs `:30-41` `_calibrator_artifact_path` used by `CalibratorFlatRegionTask`
  (`cal_cfg.artifact_path` **or** `panel_cfg.calibrator_artifact_path` **or**
  default).
- Mechanism: when `global_calibration.artifact_path` is unset but
  `panel_ltr.calibrator_artifact_path` IS set, the two gates validate two
  different files. P-CALIBRATOR-HEALTH can pass/fail on the wrong (default)
  artifact while the live scorer + flat-region gate use the panel path.
  Comment claims "parity with legacy" but the divergence is real evidence-
  source drift.
- Suggested fix: both tasks resolve through the single
  `_calibrator_artifact_path` helper.

### M-4 — `BrokerFillFreshnessTask` uses wall-clock `date.today()`, not the run date; fails closed
- File: `src/renquant_pipeline/kernel/preflight_pipeline/tasks/broker_fill_freshness.py:87`
  (`today = _dt.date.today()`); `PreflightContext` has no `today` field
  (`ctx.py:19-32`).
- Mechanism: the dormancy streak is computed against wall-clock today, so any
  run whose intended trade date ≠ execution wall-clock date (replay, delayed
  live cron, backtest gate) miscounts staleness. Worse, the
  no-activity/hard-cap branches return `("hard", False)` with NO sell-only
  relaxation — a dormant book HARD-fails preflight and (in strict mode) raises
  `PreflightFailed`, blocking the very sell-only risk exits it should permit.
- Suggested fix: add `today` to `PreflightContext` and use it; route the
  dormancy hard-fail through `_soft_for_sell_only` so sell-only exits survive.

### M-5 — `max_hold` / decay use calendar days; streak uses NYSE trading days (mixed clocks)
- File: `src/renquant_pipeline/kernel/exits.py:655` (`max_hold` calendar),
  `:524`/`:538` (`stop_decay` + σ-√t use `(today-entry).days` calendar) vs
  `:677` (`check_model_sell` NYSE trading days).
- Mechanism: tenure-based rules mix calendar and trading-day clocks. A
  `max_hold_days=60` (calendar) is ~42 trading sessions, not 60; the σ-stop
  √t and stop-decay use calendar days too. Inconsistent with the explicitly
  trading-day streak logic and with forward-label horizons
  (`nyse_trading_days_between`, used for fwd_60d).
- Suggested fix: standardize tenure rules on NYSE trading days (the repo
  already has `nyse_trading_days_between`).

---

## LOW

### L-1 — Top-up `entry_price` averaging raises cost basis; lots not updated on legacy path
- File: `src/renquant_pipeline/kernel/pipeline/task_execution.py:334-343`.
- Mechanism: the legacy top-up branch recomputes `entry_price` as a
  volume-weighted average and updates `high_watermark = max(hwm, fill.price)`
  but does NOT append a `TaxLot` (it bypasses `apply_buy_lot`). Holdings that
  mix this path with the lot-aware path get a `hs.lots` that disagrees with
  `hs.entry_price` / `hs.shares`. Stops read the averaged `entry_price`
  (raising the cost basis after a higher top-up makes the stop trigger
  earlier). Functionally tolerable but a books-consistency hazard.
- Suggested fix: route top-ups through `apply_buy_lot`.

### L-2 — DN no-trade band uses `pi_star` fallback 0.05 for sell-only / zero-target names
- File: `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:3133-3143`.
- Mechanism: when the QP target weight is ≤0 (full close / sell-only name) the
  Davis-Norman band substitutes `pi_star_i = 0.05`. The DN width is
  π·(1-π)²-shaped, so a closing name is gated as if it were a 5%-target hold.
  Usually harmless (full closes have large |Δw|) but it means the *exit* band
  width is an arbitrary constant, not tied to the actual disposed weight —
  interacts with H-4 to suppress small DN-gated trims.
- Suggested fix: for sells, use `|w_current|` (the weight being reduced) as
  π*, not a constant.

### L-3 — `_solve_cvx` catches bare `Exception` and treats it as "try next solver"
- File: `src/renquant_pipeline/kernel/portfolio_qp/qp_solver.py:101-104`.
- Mechanism: `except (cp.error.SolverError, Exception)` swallows ALL
  exceptions (including programming errors / bad inputs) as a solver miss,
  returning `"exception:<Type>"` → the QP silently no-trades instead of
  surfacing a real bug. Defensible for robustness but masks input-construction
  errors upstream (e.g. shape mismatches that should fail loud).
- Suggested fix: narrow the catch to solver/numeric exceptions; let
  programming errors propagate.

---

## NOT A BUG (verified intentional)

- **NB-1** — No-sell mask `dw[nsm] >= 0` only applied to holdings within hard
  cap (`qp_solver.py:360-369`; mask built in `ApplySoftSellGuardMaskTask`
  with the `w_current > w_hard` skip at `tasks.py:1441-1442`). Over-cap
  holdings deliberately stay sellable for #123 cap-compliance. Correct.
- **NB-2** — `turnover_exempt_forced_trims` `forced = min(0, w_upper -
  w_current)` (`qp_solver.py:382-383`) correctly isolates only the mandatory
  over-cap trim component from the turnover budget; opt-in, matches the
  documented 2026-06-09 deadlock fix. Correct.

---

## Reproduction notes

All repros run from `renquant-pipeline/src` with the RenQuant venv. Trade
evidence queried from `RenQuant/data/runs.alpaca.db` (`trades` table:
`exit_reason`, `pnl_pct`, `hold_days`, `regime`). Exit-reason aggregate at
audit time:

```
model_sell      4426  avg pnl +0.106  avg hold 27.9d
rotation         417  avg pnl +0.091  avg hold 47.1d
stop_loss        370  avg pnl -0.119  avg hold 42.5d   <- fires on 60d thesis
max_hold         258  avg pnl +0.193  avg hold 73.4d
kelly_trim       250  avg pnl +0.054  avg hold 17.0d
single_day_loss  137  avg pnl +0.090  avg hold 21.7d   <- avg POSITIVE = whipsaw
trailing_stop    121  avg pnl +0.207  avg hold 155.3d
```
