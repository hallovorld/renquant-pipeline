# Admission shadow logger — observe-only panel-vs-tournament delta (M5/R1)

DATE: 2026-07-02
STATUS: implemented (observe-only; zero behavior change)
PLAN: orchestrator `doc/design/2026-07-02-unified-107-master-plan.md` Term TC
row M5; lineage `doc/design/2026-07-02-104-capability-program.md` §3 R1.

## Why

Buy admission gates on the legacy per-ticker tournament artifacts
(`LoadUniverseJob` → `FilterStalenessTask` → `ctx.models`). The tournament
retrain is timeout-fragile: when it froze (61 days stale by 2026-06-30) the
whole book had 0 buy candidates for weeks while the panel scorer's features
were perfectly fresh. R1 proposes retiring the tournament as the admission
gate — admission derives from the panel scorer's coverage + data health.
Migration protocol: shadow the panel-based admission set against the
tournament set for N sessions; cut over only when the delta is understood.

## What

`kernel/pipeline/task_admission_shadow.py::AdmissionShadowLoggerTask`, hooked
in `pp_inference.py` inside the post-`PanelScoringJob` block (after the
existing admission AND panel scoring have both run). Per session it appends
ONE record to `logs/admission_shadow.jsonl` (`schema: admission_shadow.v1`,
default under `config["_strategy_dir"]`; override
`admission_shadow.path`): date, n_tournament, n_panel, added[], dropped[],
per-name reasons (tournament rejection reason vs panel basis/reason), broker,
regime, panel_state.

Panel-based admissibility per name (evidence-tiered, recorded via
`panel_basis` so measured vs inferred stays separable in the R1 analysis):

1. measured YES — finite score in this run's panel cross-section
   (`ctx._panel_scores_all` / `ctx.panel_scores`);
2. measured NO — panel-machinery block reason for the name, or a book-wide
   panel fail-close (`_panel_scoring_contract_failed`) → empty panel set;
3. proxy — the name never reached the panel (tournament rejected it
   upstream): OHLCV lag vs session date, fresh within
   `admission_shadow.max_ohlcv_lag_days` (default 3).

Names dropped by NON-panel buy gates (wash-sale, earnings, risk gates,
weak-score vetoes) with fresh inputs count as panel-admissible — funnel
outcomes are not admission facts and must not flood the delta.

## Contract

- OBSERVE-ONLY: the live admission still rules; the task mutates nothing but
  its own two counters (`admission_shadow_logged` / `admission_shadow_errors`)
  — pinned by a regression test.
- FAIL-ISOLATED: any exception is swallowed, logged, and counted; default-ON
  is acceptable only because of this. Kill switch:
  `admission_shadow.enabled=false`. No other new config required (both sets
  derive from existing pipeline context).
- APPEND-ONLY JSONL.

## Acceptance for R1

≥ 20 sessions of deltas accumulated, then the retirement decision reads:

```bash
jq -r 'select(.schema=="admission_shadow.v1")
       | [.date, .panel_state, .n_tournament, .n_panel,
          (.added|length), (.dropped|length)] | @tsv' \
  logs/admission_shadow.jsonl
```

plus per-name reason histograms over `.reasons` (added-by-basis,
dropped-by-basis) to confirm the delta is understood before any cutover.
Rollback stance per R1: tournament kept read-only for one quarter after any
cutover.
