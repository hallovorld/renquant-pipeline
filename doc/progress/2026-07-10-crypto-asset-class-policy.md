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
  calendar lives in renquant-common (companion PR #27, common owns calendars
  — RFC M2); the pipeline delegates to it UNCONDITIONALLY and **fails
  closed** with a clear error when the installed common predates the mode
  (< 0.11.0). There is deliberately NO local re-implementation — a
  pipeline-side fallback would fork the shared calendar, the exact hazard
  the canonical module exists to prevent (Codex re-review). Structural
  dependency bumped to `renquant-common>=0.11.0`; **merge order: common #27
  FIRST, then this PR.**
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
artifact/env issue, unrelated). Suite verified against the companion
common #27 branch state (which this PR structurally requires); the
fail-closed path is tested with the common capability explicitly masked
(`test_fails_closed_when_common_lacks_always_open_mode`), and an
integration test pins pipeline == shared-calendar for naive-UTC and
aware-offset instants around UTC midnight
(`test_delegates_to_shared_calendar_around_utc_midnight`).

## Cross-repo

Companion PR in renquant-common: `feat(calendar): ALWAYS_OPEN session mode
for 24/7 asset classes` (#27, 0.10.0 → 0.11.0) — the canonical calendar
mode this PR consumes unconditionally (fail-closed below 0.11.0; dependency
pin bumped). **MERGE ORDER: common #27 first, then this PR** — until then
the crypto P1 tests fail closed by design against a pre-#27 common.

## Update 2026-07-10: P5 ticker-scoped hardening (Codex re-review)

Codex flagged a blocking gap in the P5 §1091 bypass as originally shipped:
it was keyed off the blanket `asset_class == "crypto"` config switch alone,
with no per-symbol validation. A tokenized security or any ambiguous
instrument mis-tagged `asset_class="crypto"` would have silently inherited
the wash-sale exemption it is not legally entitled to.

**Fix**: the bypass now requires BOTH `asset_class == "crypto"` AND the
ticker being an explicitly validated non-security spot pair.

- No existing cross-repo "genuine crypto spot pair" registry was found to
  reuse (`renquant-execution`'s `CryptoAssetSpec` is order-grid metadata,
  not a security-classification source, and lives in the wrong repo for
  this kernel-level tax gate). Introduced a new operator-curated strategy
  config key, `crypto_spot_pairs` (a list), as the fail-closed source of
  truth — absent/empty ⇒ nobody is validated ⇒ §1091 still applies to
  every ticker, the safe default.
- New `kernel/asset_class.py` primitives: `resolve_validated_crypto_spot_pairs`
  (reads + normalizes the config list via the newly-available
  `renquant_common.pair_slug.as_pair`, dropping malformed entries rather
  than mis-parsing them into a false match), `is_validated_crypto_spot_pair`,
  and `wash_sale_applies_for_ticker(asset_class, ticker,
  validated_crypto_pairs)` — the new ticker-scoped decision function.
  `wash_sale_applies(asset_class)` is kept unchanged (still correct for the
  handful of gaps that are genuinely asset-class-only) but its docstring
  now warns it is insufficient alone for the §1091 bypass specifically.
- Threaded through every P5 consumer: `is_wash_sale_blocked` /
  `is_wash_sale_blocked_with_cost` (`kernel/selection.py`, both gained a
  `validated_crypto_pairs` kwarg), `SelectionContext` (new
  `validated_crypto_spot_pairs` field), `WashSaleFilterTask`
  (`task_candidates.py`), `_compute_qp_wash_mask` / `ComputeWashSaleMaskTask`
  (`portfolio_qp/tasks.py`, new `_ctx_validated_crypto_pairs` helper).
- Self-identified second hole while implementing the above:
  `StampWashSaleTask` (`task_execution.py`) computed its stamp-or-not
  decision ONCE per run at the asset-class level, before the per-fill loop
  — an unvalidated "crypto"-tagged ticker would skip stamping its sell date
  entirely, leaving nothing for the (now correctly fail-closed) block check
  to compare a re-entry against. Moved the decision inside the per-fill
  loop using `wash_sale_applies_for_ticker`, so an unvalidated ticker still
  gets its `last_sell_dates` stamped like an equity would.
- Out of scope, observed but unchanged: `task_joint_actions.py` and
  `task_rotation.py` call `is_wash_sale_blocked_with_cost` without ever
  threading `asset_class` (implicit `"us_equity"` default) — pre-existing
  equity-only gaps, unaffected by this bug or its fix either way.

**Tests** (`tests/test_asset_class_policy.py::TestP5WashSaleBypass`, 7
methods, +2 net new): every method now proves BOTH branches — a genuine
validated spot pair (`"BTC/USD"`, declared via `crypto_spot_pairs`) gets the
bypass, while an `asset_class="crypto"`-tagged but unvalidated ticker
(`"XYZ-TOKEN"`, modeling a tokenized-security-style instrument routed
through the crypto asset class) still gets blocked at every consumer.
New standalone method `test_asset_class_crypto_alone_is_not_sufficient_fail_closed`
pins the core regression directly at the `kernel/asset_class.py` level,
including the historical call shape with no `validated_crypto_pairs`
argument at all (`wash_sale_applies_for_ticker("crypto", "BTC/USD", None)
is True`). `TestP6TaxPropertyMode`'s existing test updated to pass
`validated_crypto_pairs` explicitly (no behavior change, just the new
required parameter for its already-validated fixture ticker).

Verified meaningful via stash-revert: stashing only the 6 source files
(keeping the new test file) makes the module fail to even import
(`ImportError: cannot import name 'is_validated_crypto_spot_pair'`),
confirming the tests are load-bearing on the fix. Full suite re-run with
the fix restored: identical 66 pre-existing failures / 36 collection errors
before and after (all missing-optional-dependency environment gaps —
`xgboost`, `cvxpy` — confirmed pre-existing on unmodified `origin/main` too,
unrelated to this change); 1086 passed after vs. 1084 before (the 2 net-new
methods).

Rebase/CI-currency check: `HEAD` (`834027a`) already sits exactly on
`origin/main` tip (`8775fec`, 0 commits behind) and already carries the
`renquant-common>=0.11.0` pin from the prior commit. CI's workflow
(`.github/workflows/ci.yml`) checks out `renquant-common` at its default
branch with no pinned `ref:`, so it always installs common's current main
fresh — pushing this fix's commit triggers exactly the fresh CI run needed
to pick up common#27 (now merged at 0.11.0); no separate merge/rebase of
this branch was needed.
