# Crypto asset-class execution policy (RFC gaps P1-P7, P11)

Date: 2026-07-10
PR: feat(crypto): asset-class execution policy (P1-P7)

## What

Implements the pipeline slice of the merged crypto trading RFC
(orchestrator `doc/design/2026-07-10-crypto-trading-rfc.md` §2.2 pipeline
gap table + §3.4), deliverable D-C6 plus the P5/P7 halves of D-C7. ONE new
first-class concept — top-level `asset_class: "crypto"` in the strategy
config — threaded once through the kernel's execution policy. **Absent key
⇒ `us_equity` ⇒ byte-identical equity behavior** (pinned per gap); unknown
values fail closed at resolve time.

New single source of truth: `kernel/asset_class.py`
(`resolve_asset_class`, `is_crypto`, `annualization_days_for`,
`settlement_days_for`, `wash_sale_applies`, `sigma_clip_bounds_for`,
`last_completed_always_open_session`).

## Per gap

- **P11 (switch)**: `StrategyConfigSchema.asset_class:
  Literal["us_equity","crypto"] = "us_equity"` (`kernel/config_schema.py`)
  — typos fail schema validation.
- **P1 (freshness clock)**: crypto freshness is judged against UTC calendar
  days (weekend bars REQUIRED; session `D` completes `D+1 00:00 UTC`), not
  NYSE sessions. Threaded: `LocalStore.has_range`/`fetch_ohlcv`
  (`kernel/data.py`), `DataFreshnessGateTask`
  (`kernel/pipeline/task_data_freshness.py`), `TypedDataFreshnessGate`
  (`kernel/typed_past/typed_data_freshness.py`). The canonical ALWAYS_OPEN
  calendar lives in renquant-common (companion PR, common owns calendars —
  RFC M2); the pipeline soft-consumes it when installed common ≥ 0.11.0 and
  otherwise computes the identical UTC-day arithmetic locally, so this PR
  does not hard-depend on the common PR's merge order.
- **P2 (hold/streak clocks)**: `kernel/exits.py` gains asset-class-aware
  `is_trading_day` / `trading_days_between` (crypto: every day trades,
  clocks count calendar days); `check_model_sell` / `compute_exits` take
  `asset_class`; `soft_exit_guards.trading_holding_days` /
  `soft_exit_horizon_suppression` dispatch likewise, threaded at all six
  call sites (task_sell, task_panel_conviction_xs, QP soft-sell guard +
  lot-age helper).
- **P3 (settlement)**: crypto is T+0 — `T2CashQueue` supports
  `settlement_days=0` (same-day drain), `T2CashQueue.for_asset_class`
  builds the right queue, and `SimBackend(asset_class="crypto")`
  structurally bypasses the queue regardless of a configured `t2_days`.
- **P4 (annualization 365)**: `compute_vol_target_scale` gains
  `annualization_days` (caller resolves 252/365 by asset class);
  `AlignQPHorizonUnitsTask` / `_qp_sigma_horizon_scale` de-annualize with
  the asset class's divisor; both `_realized_vol_annualized` copies
  (panel fallback + `RealizedVolGateTask`) annualize with √365 for crypto.
- **P5 (wash-sale bypass — crypto is property, §1091 N/A)**: keyed off the
  asset class at the single source of truth
  (`is_wash_sale_blocked{,_with_cost}` in `kernel/selection.py`) and at
  every consumer: `WashSaleFilterTask` (candidates), `_compute_qp_wash_mask`
  / `ComputeWashSaleMaskTask` (QP Δw≤0 mask — §1091 leg only; min-reentry
  anti-churn + calibrator-saturation legs are risk controls and still
  apply), `run_selection_loop` (`SelectionContext.asset_class`), and
  `StampWashSaleTask` — **a crypto sell stamps NO `last_sell_dates`
  re-entry state at all** (the G8 post-stop cooldown stamp, a risk rail,
  still fires). Explicit RFC-required test: crypto sell does not
  stamp/block re-entry while an equity one still does.
- **P6 (tax property-mode)**: verify-only per the RFC — ST/LT 365-day
  holding-period treatment untouched and available to crypto; pinned by
  test (no code change).
- **P7 (σ-clip bounds per asset class)**: realized-σ clip DEFAULTS become
  asset-class-aware — us_equity keeps [0.05, 1.50] byte-identically,
  crypto defaults to [0.20, 3.00] annualized-365 (RFC §3.4 frozen: the
  1.50 ceiling must not pin 60-150%+ crypto vol or Kelly cannot
  discriminate). Explicit `realized_vol_floor`/`realized_vol_ceiling`
  config keys still win for both classes.

## Explicitly out of scope (per brief)

- No scorer/label work (D-C3/C4), no fee-gate work (D-C8a/b), no P8/P10
  fundamentals/sector/SPY-regime gate bypasses (rest of D-C7), no P9 symbol
  slugs.
- `global_calibrator.py` ER ±0.20 load-clip left untouched: it is an
  artifact-load sanity clip whose crypto value belongs with the crypto
  calibrator artifact (D-C9), and the RFC's frozen P7 resolution names only
  the σ clip. Recorded as deferred, not forgotten.
- Portfolio vol-target for crypto is ABSOLUTE-only per the RFC's frozen
  resolution; this PR only makes the annualization correct — the crypto
  strategy config (D-C10) decides whether the SPY-proxied `vol_target`
  block is enabled at all (it strips it).

## Tests

`tests/test_asset_class_policy.py` — 31 new tests, one class per gap, each
carrying an equity byte-identity pin (absent `asset_class` reproduces
legacy behavior exactly). Full suite: 1532 passed / 8 skipped / 1
pre-existing environment failure (`test_xgboost_scorer_contract.py::
test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`
fails identically on clean origin/main in this environment — xgboost
artifact/env issue, unrelated). New tests verified BOTH against common
main (fallback path) and against the companion common branch (ALWAYS_OPEN
consume path).

## Cross-repo

Companion PR in renquant-common: `feat(calendar): ALWAYS_OPEN session mode
for 24/7 asset classes` (0.10.0 → 0.11.0) — canonical calendar mode; this
PR degrades gracefully without it (no pin bump required).
