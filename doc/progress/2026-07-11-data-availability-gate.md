# 2026-07-11 — generic pre-decision DATA-AVAILABILITY & VINTAGE gate

## Design (lean)

**State: operator-mandated.** The META / 07-08 investigations exposed that
input-integrity checking is FRAGMENTED — five independent mechanisms, five
different failure modes:

| today's fragment                          | failure mode it allowed |
|-------------------------------------------|-------------------------|
| `DataFreshnessGateTask` (OHLCV, fail-closed per symbol) | the GOOD pattern — kept unchanged, still authoritative for session-aware OHLCV staleness |
| admission staleness gate (`job_universe.FilterStalenessTask`, fail-closed per ticker) | correct per ticker, but the 07-08 AGGREGATE collapse (133/145 stale → buy scan on ~0 tickers) went out as a normal no-trade |
| P-FUND-FRESHNESS (preflight) | structurally unsatisfiable for ~88d without anyone noticing (the serving-axis clip bug; base-data #26 + pipeline #151) |
| P-MODEL-STALENESS (preflight) | SOFT-SKIP — a model trained to 2024-11 served silently (the 2026-06-26 incident) |
| (nothing) | whole-dataset ABSENCE had no check at all (the SGOV case) |

**This PR adds ONE general gate**: `DataAvailabilityGateTask`, running EARLY
in `InferencePipeline` (after the OHLCV freshness gate, before `RegimeJob` —
i.e. before any scoring or decision logic). It is the **input-side complement
of `FunnelIntegrityTask` (#186)**: FunnelIntegrity classifies the OUTPUT
funnel at the END of the run; this gate verifies the INPUTS at the START.
No overlap in responsibility — it never re-classifies outcomes.

Per declared **input axis** it verifies three facts: **presence**, **as-of
vintage** vs the axis's declared freshness budget, and **universe coverage**
fraction.

**Behavior-invariant** except for axes an operator EXPLICITLY declares
`policy: fail_closed` — and no axis defaults to fail_closed on day one (all
default `degrade_with_alarm`), so prod cannot be darked by the rollout.

### Axes v1 (built-in; checked with safe defaults even when undeclared)

| axis | presence | vintage | coverage | default budget | default policy |
|---|---|---|---|---|---|
| `ohlcv_bars` | per expected symbol (reuses `DataFreshnessGateTask._expected_symbols`) | max bar date | fraction fresh | 5d / cov 1.0 | degrade_with_alarm |
| `fundamentals_serving_axis` | serving parquet exists | global + PER-SYMBOL as-of | fraction of watchlist within budget | 20d / cov 0.80 (== DataVerificationTask / P-FUND-FRESHNESS) | degrade_with_alarm |
| `panel_model_artifact` | file exists + fingerprint resolvable (stamped, or SHARED `renquant_common.model_fingerprint` recompute — never a local re-fork) | `trained_date` + binding train cutoff (`job_universe.TRAINING_DATA_FIELDS` via its own reader) | — | 120d train / 335d cutoff (== P-MODEL-STALENESS rails) | degrade_with_alarm (**the soft-skip is now a real config-keyed policy**) |
| `calibrator` | required-calibration resolvable BEFORE scoring (`missing_global_calibration` signature), method/params sane | fit date when stamped | — | none (opt-in) | degrade_with_alarm |
| `admission_model_metadata` | admitted set non-empty | binding cutoffs, REUSING `job_universe._classify_cutoffs` / `_resolve_axes` (not a duplicate) | admitted+fresh fraction of watchlist | cov 0.5 (== umbrella #463 `universe_collapse_floor_frac`) | degrade_with_alarm |
| `regime_inputs` | benchmark (SPY) bars present | benchmark max bar date | 1-of-1 | 5d | degrade_with_alarm |
| `account_snapshot` | portfolio/cash/holdings snapshot present | `ctx.account_snapshot_at` when stamped (missing stamp surfaced as evidence — provenance gap made visible) | — | 1440 min | degrade_with_alarm |
| custom `kind: dataset_file\|dataset_dir` | dataset exists (the SGOV class) | optional `date_column` + `max_staleness_days`; optional sealed-`manifest` fingerprint presence (base-data crypto_bars / D-C2 pattern) | — | declared | degrade_with_alarm |

### Contracts: declared, not hardcoded

Versioned `config["data_contracts"]` section (schema `data_contracts.v1`),
shape consistent with renquant-base-data's dataset manifests (axis id +
freshness rule + how validated): per axis `max_staleness_days` /
`min_coverage` / `policy` / axis-specific keys. A consumed built-in axis with
NO declared contract is still checked with defaults and the gate warns
LOUDLY (`missing_contracts` in the block) — so new inputs get contracts.

### Fail policy, honoured per axis

* `fail_closed` — violated (or UNVERIFIABLE — a checker crash is a fail, not
  a pass) axis aborts the run loudly, exactly like `DataFreshnessGateTask`.
* `degrade_with_alarm` — the day-one default for every axis: run proceeds;
  alarm lands in `ctx.data_availability` (run bundle) + `ctx.counters`
  (`data_availability_fired/degraded/blocked`) and is ntfy-visible via
  `notification_fields()` — the same stamping pattern as umbrella #463's
  `universe_health` + `universe_collapse` (top-level `degraded` bool). This
  PR only EMITS the fields; the umbrella wires them (not edited here).
  Checker crashes are fail-isolated under this policy.

### Output

`ctx.data_availability`, schema `data_availability.v1` — same reporting
plane and field style as `funnel_integrity.v1` (#186): `verdict
(AVAILABLE|DEGRADED|BLOCKED), degraded, blocked, axes{...per-axis verdict /
policy / as_of / age_days / coverage / violations / evidence / effective
contract...}, fired[], axes_evaluated[], missing_contracts[], error`.

Kill switch `data_availability.enabled=false`; sell-only runs skip (buy-input
verification must never block the risk-exit path — same reasoning as the
P-FUND-FRESHNESS sell-only exemption and FunnelIntegrityTask's sell-only
skip). Verify-only: zero decision-logic change.

## Artifacts

* `src/renquant_pipeline/kernel/pipeline/task_data_availability.py` — the
  gate, axis checkers, contracts handling, notification adapter.
* `src/renquant_pipeline/kernel/pipeline/pp_inference.py` — wiring (after
  `DataVerificationTask`, before `RegimeJob`; `InferencePipeline` only).
* `tests/test_data_availability.py` — 53 tests.

## Verification

* Every incident signature has a test: stale fundamentals serving axis
  (88d), ancient model vintage (2024-11 cutoff), whole-dataset absence
  (SGOV), admission coverage collapse (07-08), required-calibrator missing,
  OHLCV missing/stale + coverage, benchmark absence, account snapshot
  absence/staleness.
* Policy mechanics: fail_closed aborts (block still stamped for the bundle
  first), degrade proceeds with alarm, mixed policies abort naming only the
  fail-closed axes, unknown policy downgrades to degrade with a warning.
* Fail isolation: per-checker crash under degrade never darks the run and
  never takes other axes dark; under fail_closed it blocks; whole-task crash
  is swallowed (error block + counter) unless a fail_closed axis is declared.
* Contracts: missing-contract loud warning, declared contracts suppress it,
  contract overrides budgets, per-axis disable, default policy pinned to
  degrade for every built-in axis.
* Behavior invariance regression pin (decision state unmutated), kill
  switch, sell-only skip, notification fields, wiring position (early,
  before `RegimeJob`; absent from `SellOnlyPipeline`).
* Full suite: **1621 passed, 7 skipped** (1568 pre-existing + 53 new), 0
  failures.

## Explicitly NOT in scope

* No edits to the umbrella (#463 fields are coordinated, not modified), the
  orchestrator, or base-data.
* No removal/weakening of the existing gates (`DataFreshnessGateTask`,
  `DataVerificationTask`, preflight P-checks) — deduplication into contracts
  is a follow-up once the gate has soaked.
* No fingerprint EQUALITY re-verification (scoring fail-close owns it; a
  fourth hand-copied comparison is exactly the calibrator/scorer triple-impl
  bug class) — stamp presence/resolvability only.
* No fail_closed default anywhere; flipping axes to fail_closed is an
  operator config decision after soak.

## 2026-07-11 update — Codex CHANGES_REQUESTED fixes

Codex's review of the first version of this PR raised three findings, all
fixed on this branch:

1. **P1 (safety, the big one): fail_closed could suppress a sell/exit.**
   `DataAvailabilityGateTask` was wired before `RegimeJob`/`BuyGatesJob`/the
   sell pass, and its `run()` raised `RuntimeError` on a fail_closed axis
   violation. That exception propagated out of `InferencePipeline.run()`
   BEFORE `TickerSellJob` ever executed — a data-availability problem could
   silently cancel a bar's risk exits. Exempting only `SellOnlyPipeline`
   didn't help: an ordinary full daily run can carry an urgent sell.
   **Fix**: split the task into `run()` (called at the same early position —
   records the verdict into `ctx.data_availability` + counters, logs loudly,
   but NEVER raises and NEVER touches `ctx.buy_blocked`) and a new
   `enforce_buy_block(ctx)` (wired AFTER `TickerSellJob` and every
   downstream exit-refining task — `DrawdownFlattenTask`,
   `MetaLabelVetoTask`, `LimitSellsPerBarTask`, `ShortCoverStopLossTask` —
   and BEFORE the Phase 2b buy-candidate scan). `enforce_buy_block` sets
   `ctx.buy_blocked = True` when a fail_closed axis fired — the same
   errata-C choke point every other buy gate uses (`job_gates.BuyGatesJob`,
   macro gates, `panel_scoring.PanelScoringJob`) — and never raises. New
   integration test `TestFailClosedNeverSuppressesSells` in
   `tests/test_data_availability.py` runs the REAL
   `InferencePipeline().run(ctx)` with a forced fail_closed violation and
   asserts a real stop_loss exit still lands in `ctx.exits` while
   `ctx.buy_blocked` is set.
2. **Contract scope: an undeclared axis could still alarm.** Previously, a
   built-in axis with no `data_contracts.axes` entry was still evaluated
   against built-in defaults, so an unreviewed axis could produce a
   DEGRADED verdict that looked authoritative. **Fix**: a missing (or
   malformed) contract entry now means the axis is NOT evaluated at all —
   the checker is never called; the axis is recorded `verdict: "unverified"`
   (new `AXIS_UNVERIFIED` constant) with no violations, and can never enter
   `fired`/`degraded`/`blocked`. Only an axis with an explicit, reviewed
   `data_contracts.axes[name]` entry can ever alarm; blocking is a further,
   separate bar — that same entry must ALSO declare `policy: fail_closed`.
   `tests/test_data_availability.py::TestContracts` covers both the
   unverified record and the (im)possibility of fail_closed on an
   undeclared axis.
3. **Repo/ownership scope: pipeline was formatting ntfy text.** The
   `notification_fields()` helper (title/tag/line rendering for the
   umbrella's ntfy adapter) has been deleted from this module. This repo now
   publishes `ctx.data_availability` (the versioned, structured block) only.
   Title/page rendering for operator-facing alerts is orchestrator
   monitor-layer territory — a follow-up consumer PR in
   `renquant-orchestrator` is expected to render it; **not implemented
   here** (out of scope for this repo, and not something the pipeline agent
   should build).

**Framing correction**: this task is NOT "behavior-invariant" — once any
axis is declared `fail_closed`, a violation DOES mutate decision state
(`ctx.buy_blocked = True`), by design. No axis defaults to fail_closed, so
day-one rollout has zero behavioural effect until an operator opts one in;
from that point on it is explicitly **buy-decision-affecting**. Any further
escalation (e.g. defaulting an axis to fail_closed more broadly) requires a
staged design + shadow evidence — that is future work, not covered by this
PR.

Full suite after the fixes: 1614 passed, 8 skipped (same 3 pre-existing
environment-version-mismatch failures unrelated to this change —
`test_replay_d6_conventions.py` numpy/scipy pin drift,
`test_xgboost_scorer_contract.py` xgboost version drift — confirmed present
before this branch's changes too).
