# FUNNEL-INTEGRITY pipeline step — first-class silent-no-buy verdict

Date: 2026-07-11
Owner: renquant-pipeline (kernel task; orchestrator/umbrella consume the output)
Mandate: operator — "pipeline 要加步骤来彻底解决" the silent-no-buy class after the
2026-07-08/09 admission outage (two sessions of zero buy capability reported
through the same quiet ntfy path as a normal weak-signal no-trade; incident
record: renquant-orchestrator PR #473, META no-buy forensics).

## Design (lean)

**Reviewer note: this is operator-mandated OBSERVABILITY and is
DECISION-INVARIANT, not zero-mutation.** The task reads final funnel state,
classifies it, and publishes a block; it never reads-then-changes any
signal, decision, sizing, or order state (regression-pinned by test) — but
it DOES mutate `ctx`: it stamps `ctx.funnel_integrity`, two `ctx.counters`
mirrors, and appends to `ctx.monitor_state` history. That mutation is the
intended, documented side effect, additive to observability state only. It
is default-ON, which is acceptable only because it is fail-isolated by the
same contract as `AdmissionShadowLoggerTask`: any exception is swallowed,
logged, counted, and stamped onto the block's `error` field — its own crash
can never dark the run it audits.

### Problem

The buy funnel has two very different "no trade" days that today emit the
same signal:

1. **Economic**: candidates existed, none cleared correctly-scaled bars.
2. **Structural**: an engineering condition suppressed buy capability
   (admission collapse, fingerprint fail-close, mis-scaled thresholds,
   wash-sale state corruption, dead data) — 07-08/09 was class 2 reported
   as class 1 for two sessions.

### Shape

`FunnelIntegrityTask` (`kernel/pipeline/task_funnel_integrity.py`) runs in
every full `InferencePipeline` run **after the final buy/rotation/exit
emission** (post-selection, post-rotation, post-monitor — so its funnel
counts are final) and **strictly before `DecisionLedgerWriteTask`** (S5),
so any current or future consumer of that persisted ledger/run record can
already see `ctx.funnel_integrity` and its counter mirrors — pinned by
`test_wired_before_decision_ledger_write` and exercised end-to-end (real
`FunnelIntegrityTask` → real `DecisionLedgerWriteTask` on one shared `ctx`)
by `test_decision_ledger_task_can_see_funnel_integrity_in_correct_order`.
Deliberately **not** wired into `SellOnlyPipeline`: the exit-only variant
has no buy funnel to judge and runs ~every 12 min intraday — a per-tick
verdict would be false-OUTAGE spam.

It assembles a read-only `FunnelView` snapshot from ctx (watchlist, admitted
universe + `_universe_rejections`, merged blocked maps, per-gate kill counts,
session mu surfaces, panel/calibrator fail-close flags, prices/OHLCV
presence, prior history), evaluates the invariant registry against it, and
classifies:

| Verdict | Meaning |
|---|---|
| `ECONOMIC_TRADE` | buys emitted; nothing fired |
| `ECONOMIC_NO_TRADE` | zero buys; nothing fired — the only verdict that may be reported as a normal no-trade |
| `DEGRADED` | invariant(s) fired but capability partially survived (buys still emitted), or warn-severity findings only |
| `STRUCTURAL_BLOCK` | structural invariant fired AND zero buys — an engineering OUTAGE, never a no-trade |

### Structural detectors (named invariants, config-keyed thresholds, safe defaults)

Config namespace: `funnel_integrity.<invariant>.<key>`; per-invariant kill
switch `enabled=false`; global kill switch `funnel_integrity.enabled=false`.

| Invariant | Incident signature | Default thresholds |
|---|---|---|
| `universe_admission_collapse` | 07-08/09: `stale_76d_limit_60:live_train_end` × 133/145 → buy scan on 0 tickers | `min_admitted_frac=0.5`, `max_staleness_rejection_frac=0.5` |
| `single_gate_funnel_kill` | one gate family kills 100% of assembled candidates → zero survivors, when history says it rarely fires | `min_kills=3`, `min_share=1.0`, `rare_fire_rate=0.25`, `min_history_sessions=10` (cold start → warn severity) |
| `threshold_scale_mismatch` | PatchTST-negative-scores era: conviction `mu_floor` above the max achievable session mu (structural when all mus ≤ 0, warn when positive-but-below); rotation `min_expected_advantage_pct` above the max session ER (warn) | reads live `conviction_gate` / `rotation` config; no own thresholds |
| `fail_close_event` | shadow config-FP incident: `panel_scorer_config_mismatch` / calibrator fail-close clearing all candidates, dark for days | fires on flags/reasons; no thresholds |
| `wash_sale_mass_block` | STATE-EXT-SELL date bug: reconciliation stamps "today" → mass §1091 blocking | `min_count=5` AND > historical p99 (`min_history_sessions=10`; cold start = absolute floor only) |
| `zero_priced_candidates` | dead price/OHLCV feed masquerading as weak signal | `max_frac=0.2`, `min_count=3`; each leg evaluated only when its source map is populated |

History for the two history-aware detectors is a compact rolling slice
(`funnel_integrity_history`, default window 60 sessions, one record per
trading day, same-date re-runs replace) on `ctx.monitor_state` — the same
adapter-persisted vehicle `MonitorIdleStreakTask` already uses. Detectors
read prior sessions only; today's record is appended after evaluation.

### Plug-in contract (for the retrospective-sweep registry)

An invariant is any object with a stable `name: str` and
`evaluate(view: FunnelView, cfg: dict) -> InvariantFinding | None`. The
sweep agent's incident classes plug in via `FunnelIntegrityTask(invariants=…)`
or a follow-up PR extending `DEFAULT_INVARIANTS`. A single detector's
exception is isolated per-detector (others still run; the block's `error`
carries it).

### Output contract (what downstream consumes)

`ctx.funnel_integrity` (schema `funnel_integrity.v1`) — exact fields:

```
schema, date, run_mode,
verdict, verdict_reason, structural,
fired[]            — {invariant, severity, reason, evidence}
invariants_evaluated[],
gate_kill_counts   — {gate_family: kill_count}
funnel             — {n_watchlist, n_admitted, n_universe_rejected,
                      n_buy_scan_blocked, n_late_candidates,
                      n_candidates_final, n_ranked, n_rotations,
                      n_buy_orders, n_exits, buy_blocked, bear_only,
                      skip_buys}
error              — None | str
```

* **Run bundle / counters_json**: downstream persistence (umbrella
  `build_run_bundle` / orchestrator run bundle) can stamp the block verbatim
  under the key `funnel_integrity`. Integer mirrors (`funnel_integrity_fired`,
  `funnel_integrity_structural`, `funnel_integrity_errors`) land on
  `ctx.counters`, so the existing `counters_json` persistence can carry the
  verdict with no schema change of its own. This module emits ONLY that
  versioned structured verdict — owner-neutral serialization, not a
  notification/paging contract.
* **Notification is explicitly out of scope for this repo.** Rendering the
  block into an operator-facing page/title (OUTAGE-vs-no-trade framing) is
  whichever repo owns notification delivery — renquant-orchestrator's
  monitor, per the same multi-repo boundary Codex enforced by closing
  RenQuant#462/#463 (broker-capability and universe-collapse alerting both
  belong to their owning repo, not the umbrella). That consumer must arrive
  as a separate renquant-orchestrator PR that reads this schema; this PR
  does not declare field names or a rendering contract for it (an earlier
  draft's `notification_headline()` helper recreated exactly the deprecated
  umbrella-integration boundary and has been removed).
* A `STRUCTURAL_BLOCK` verdict also emits a `FunnelIntegrityAlert` WARNING
  log line so existing log-scraping alert paths see it immediately, before
  any consumer wiring lands.

### Boundaries honored

No signal/decision logic changes, no order-path changes, no umbrella
imports; observe/classify/report only. Admission enforcement stays owned by
`job_universe.py`; panel fail-close stays owned by `job_panel_scoring.py` —
this task only OBSERVES their outcomes. Alert rendering is explicitly NOT
this module's job (see Notification above) — that ownership boundary is
enforced the same way RenQuant#462/#463 were.

## What was done

* `src/renquant_pipeline/kernel/pipeline/task_funnel_integrity.py` — new
  task + 6 detectors + `FunnelView`/`InvariantFinding` plug-in contract +
  rolling history. Emits only the versioned structured verdict; no
  notification/paging helper lives here.
* `src/renquant_pipeline/kernel/pipeline/pp_inference.py` — wired after the
  final buy/rotation/exit emission and strictly before
  `DecisionLedgerWriteTask` (observe-only, fail-isolated; NOT in
  `SellOnlyPipeline`, reason documented inline).
* `tests/test_funnel_integrity.py` — 35 tests: each detector fires on a
  synthetic context reproducing its incident signature (07-08 staleness
  collapse with the exact 133/145 `stale_76d_limit_60:live_train_end`
  numbers; config-FP clear; PatchTST-all-negative threshold mismatch;
  wash-sale mass-block vs history p99), suppression counter-cases,
  clean-session `ECONOMIC_NO_TRADE`/`ECONOMIC_TRADE` verdicts, `DEGRADED`
  partials, detector-level and task-level fail-isolation (run unaffected,
  block carries the error), a decision-invariant regression pin (decision
  fields byte-for-byte unchanged, observability state IS mutated), kill
  switches, history persistence/retry semantics (same-date re-run
  replaces, not duplicates), the corrected pp_inference wiring-order pin,
  and an integration test exercising the real `FunnelIntegrityTask` →
  real `DecisionLedgerWriteTask` call sequence on one shared `ctx` to prove
  the ledger task's call site can already see the published block.

### Round-2 fixes (Codex review, commit 6212c580 → this revision)

Codex requested changes on three points; all three are addressed above:

1. **Task ordering.** `FunnelIntegrityTask` was wired AFTER
   `DecisionLedgerWriteTask`, so its block/counters could never be present
   in that ledger write despite the PR's persistence claims. Moved to run
   before it (still after all buy/rotation/exit emission); added the
   integration test described above (a genuine end-to-end
   `InferencePipeline().run(ctx)` test is not possible in this repo today —
   `kernel.meta_label.task_meta_label_veto` / `job_meta_label_log` are
   still umbrella/renquant-backtesting-only, unconditionally imported by
   `InferencePipeline.run`, same constraint `test_lift_pp_inference.py`
   already documents for the xgboost boundary; flagged as a pre-existing,
   out-of-scope migration gap, not touched here).
2. **Ownership boundary.** `notification_headline()` declared field names
   for an umbrella/orchestrator consumer, recreating the deprecated
   integration boundary Codex already closed via RenQuant#462/#463. Removed
   entirely; the module docstring now states this repo emits only the
   versioned structured verdict, and alert rendering is explicitly deferred
   to a separate renquant-orchestrator PR.
3. **Behavior framing.** "ZERO behavior change" was inaccurate — the task
   does mutate `ctx.funnel_integrity`, `ctx.counters`, and
   `ctx.monitor_state`. Reframed as DECISION-INVARIANT (no decision/order
   field is ever read-then-changed) throughout the module docstring, class
   docstring, and this doc; the regression-pin test now asserts both halves
   (decision fields unchanged AND the new observability state IS
   published), and persistence/retry semantics (same-date replace, not
   duplicate) are explicit in the docstring and covered by existing history
   tests.

## Evidence

* `tests/test_funnel_integrity.py`: **35 passed** (isolated worktree,
  after the round-2 fixes above). `[VERIFIED]`
* `tests/test_funnel_integrity.py` + `test_lift_pp_inference.py` +
  `test_task_decision_ledger.py` + `test_decision_ledger.py`: **66 passed**.
  `[VERIFIED]`
* Full pipeline suite (excluding pre-existing `cvxpy`-dependent modules,
  an environment gap unrelated to this change): **1329 passed, 8 skipped**,
  identical failure set (30, all `cvxpy`/environment-related) before and
  after this revision's changes — diffed by stashing/unstashing against
  the same worktree commit. `[VERIFIED]`
* No production paths touched; work done in an isolated worktree; no git
  operations in the live umbrella tree or primary checkouts. `[VERIFIED]`

## Follow-ups (not this PR)

* renquant-orchestrator: a separate PR to read `ctx.funnel_integrity` /
  `funnel_integrity.v1` and render the OUTAGE-vs-no-trade page/title (the
  #473 follow-up "page on buy-scan universe collapse as an OUTAGE"). No
  field-name contract is pre-declared here; that PR designs its own
  consumer against the schema documented in this module's docstring.
* Umbrella / orchestrator run-bundle builder: stamp `ctx.funnel_integrity`
  into `build_run_bundle` (currently does not reference it at all —
  confirmed by reading `kernel/artifact_contract.py` during this fix).
* Retrospective-sweep registry: plug additional incident classes in via the
  invariant contract.
* After ~2 weeks of history accumulation, review `single_gate_funnel_kill`
  and `wash_sale_mass_block` thresholds against observed fire rates.
