# FUNNEL-INTEGRITY pipeline step — first-class silent-no-buy verdict

Date: 2026-07-11
Owner: renquant-pipeline (kernel task; orchestrator/umbrella consume the output)
Mandate: operator — "pipeline 要加步骤来彻底解决" the silent-no-buy class after the
2026-07-08/09 admission outage (two sessions of zero buy capability reported
through the same quiet ntfy path as a normal weak-signal no-trade; incident
record: renquant-orchestrator PR #473, META no-buy forensics).

## Design (lean)

**Reviewer note: this is operator-mandated OBSERVABILITY and is
behavior-invariant — it is NOT a staged-rollout behavior change.** The task
reads final funnel state, classifies it, and publishes a block; it mutates no
signal, decision, sizing, or order state (regression-pinned by test). It is
default-ON, which is acceptable only because it is fail-isolated by the same
contract as `AdmissionShadowLoggerTask`: any exception is swallowed, logged,
counted, and stamped onto the block's `error` field — its own crash can never
dark the run it audits.

### Problem

The buy funnel has two very different "no trade" days that today emit the
same signal:

1. **Economic**: candidates existed, none cleared correctly-scaled bars.
2. **Structural**: an engineering condition suppressed buy capability
   (admission collapse, fingerprint fail-close, mis-scaled thresholds,
   wash-sale state corruption, dead data) — 07-08/09 was class 2 reported
   as class 1 for two sessions.

### Shape

`FunnelIntegrityTask` (`kernel/pipeline/task_funnel_integrity.py`) runs at
the **end of every full `InferencePipeline` run** (after
`DecisionLedgerWriteTask`, before the DONE log). Deliberately **not** wired
into `SellOnlyPipeline`: the exit-only variant has no buy funnel to judge and
runs ~every 12 min intraday — a per-tick verdict would be false-OUTAGE spam.

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

* **Run bundle**: downstream persistence (umbrella `build_run_bundle` /
  orchestrator run bundle) stamps the block verbatim under the key
  `funnel_integrity`. Integer mirrors (`funnel_integrity_fired`,
  `funnel_integrity_structural`, `funnel_integrity_errors`) land on
  `ctx.counters`, so the existing `counters_json` persistence carries the
  headline with zero consumer changes.
* **Notification**: the umbrella ntfy titles OUTAGE vs no-trade via
  `notification_headline(getattr(ctx, "funnel_integrity", None))` →
  `{"outage": bool, "title_tag": "OUTAGE"|"DEGRADED"|"NO-TRADE"|"TRADE"|
  "UNKNOWN", "line": str}`. Coordination is by CONTRACT: this PR emits the
  fields; the umbrella universe-collapse-alert PR consumes them (no open
  umbrella PR existed at authoring time — checked 2026-07-11 — so these
  names are the canonical ones for that agent to consume; a
  `STRUCTURAL_BLOCK` verdict / `outage=true` is the page trigger for the
  #473 follow-up "page on buy-scan universe collapse as an OUTAGE").
* A `STRUCTURAL_BLOCK` verdict also emits a `FunnelIntegrityAlert` WARNING
  log line so existing log-scraping alert paths see it immediately, before
  any consumer wiring lands.

### Boundaries honored

No signal/decision logic changes, no order-path changes, no umbrella
imports; observe/classify/report only. Admission enforcement stays owned by
`job_universe.py`; panel fail-close stays owned by `job_panel_scoring.py` —
this task only OBSERVES their outcomes.

## What was done

* `src/renquant_pipeline/kernel/pipeline/task_funnel_integrity.py` — new
  task + 6 detectors + `FunnelView`/`InvariantFinding` plug-in contract +
  `notification_headline` + rolling history.
* `src/renquant_pipeline/kernel/pipeline/pp_inference.py` — wired at the end
  of `InferencePipeline.run` (observe-only, fail-isolated; NOT in
  `SellOnlyPipeline`, reason documented inline).
* `tests/test_funnel_integrity.py` — 35 tests: each detector fires on a
  synthetic context reproducing its incident signature (07-08 staleness
  collapse with the exact 133/145 `stale_76d_limit_60:live_train_end`
  numbers; config-FP clear; PatchTST-all-negative threshold mismatch;
  wash-sale mass-block vs history p99), suppression counter-cases,
  clean-session `ECONOMIC_NO_TRADE`/`ECONOMIC_TRADE` verdicts, `DEGRADED`
  partials, detector-level and task-level fail-isolation (run unaffected,
  block carries the error), zero-behavior-change regression pin, kill
  switches, history maintenance, notification contract, and the
  pp_inference wiring pin.

## Evidence

* Full pipeline suite: **1603 passed, 7 skipped** (was 1568 before; +35 new)
  — `make test` in an isolated clone, 2026-07-11. `[VERIFIED]`
* No production paths touched; work done in an isolated clone; no git
  operations in the live umbrella tree or primary checkouts. `[VERIFIED]`

## Follow-ups (not this PR)

* Umbrella: consume `notification_headline` in `_notify_decision` and stamp
  `ctx.funnel_integrity` into `build_run_bundle` (owned by the
  universe-collapse-alert agent's PR; contract above).
* Retrospective-sweep registry: plug additional incident classes in via the
  invariant contract.
* After ~2 weeks of history accumulation, review `single_gate_funnel_kill`
  and `wash_sale_mass_block` thresholds against observed fire rates.
