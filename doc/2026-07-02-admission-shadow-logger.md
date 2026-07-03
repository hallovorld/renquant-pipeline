# Admission shadow logger — observe-only panel-vs-tournament delta (M5/R1)

DATE: 2026-07-02
STATUS: implemented (observe-only; zero behavior change)
PLAN: orchestrator `doc/design/2026-07-02-unified-107-master-plan.md` Term TC
row M5; lineage `doc/design/2026-07-02-104-capability-program.md` §3 R1.

## Round 2 (review response, 2026-07-02)

WHAT: review found the headline panel-vs-tournament delta blended measured
panel-admit evidence with proxy-inferred rows (names the tournament blocked
before they ever reached the panel), risking a retirement case built from
inference mass rather than observed panel behavior. Fixed by structurally
separating `added_measured`/`dropped_measured` (the R1 decision metric) from
`added_proxy`/`dropped_proxy` (reported, never pooled), stamping a
`taxonomy_version` on every record, and adding `build_acceptance_packet()` to
freeze the 20-session analysis packet with pooled (not per-session-averaged)
denominators and split reason histograms. See "Measured vs proxy" and
"Acceptance for R1" below.
EVIDENCE: 8 new tests (measured/proxy split, basis partition, acceptance
packet headline/denominators/histograms/taxonomy-mismatch-rejection/empty
window); full suite 1062/1062 passed, 7 pre-existing skips.
NEXT: none for this PR — the packet function exists so the actual R1
retirement decision (after ≥ 20 real sessions accumulate) reads a frozen,
measured-only metric rather than an ad hoc jq one-liner.

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

### Measured vs proxy (review directive, 2026-07-02)

A name the tournament blocked before it ever reached the panel is not the
same evidence as a name the panel actually scored — even when the OHLCV
freshness proxy says it looks eligible. Blending the two would let inference
volume (tournament blocks far more names than the panel ever scores)
manufacture a retirement case that observed panel behavior doesn't support.

So every record carries the split explicitly:

- `added_measured` / `dropped_measured` (bases `panel_scored`,
  `panel_block`, `panel_fail_closed`) — **the R1 headline decision metric**.
- `added_proxy` / `dropped_proxy` (bases `input_fresh_proxy`,
  `input_freshness`) — reported for visibility, never pooled into the
  headline.
- `added` / `dropped` — the full measured ∪ proxy union, kept for
  eyeballing a single session, **not** the decision metric.

Each record also stamps `taxonomy_version` (`admission_shadow_taxonomy.v1`).
The taxonomy (which basis values exist, and which of MEASURED_BASES /
PROXY_BASES each falls into) only changes on a version bump — it cannot
silently drift mid-window.

## Contract

- OBSERVE-ONLY: the live admission still rules; the task mutates nothing but
  its own two counters (`admission_shadow_logged` / `admission_shadow_errors`)
  — pinned by a regression test.
- FAIL-ISOLATED: any exception is swallowed, logged, and counted; default-ON
  is acceptable only because of this. Kill switch:
  `admission_shadow.enabled=false`. No other new config required (both sets
  derive from existing pipeline context).
- APPEND-ONLY JSONL.

## Acceptance for R1 — frozen 20-session packet

The retirement decision does NOT read raw `added`/`dropped` counts (those
blend measured and proxy evidence). It reads
`task_admission_shadow.build_acceptance_packet(records)` over ≥ 20 sessions
of accumulated deltas, e.g.:

```python
import json
from renquant_pipeline.kernel.pipeline.task_admission_shadow import (
    build_acceptance_packet,
)

records = [json.loads(line) for line in open("logs/admission_shadow.jsonl")]
packet = build_acceptance_packet(records)
```

The packet is frozen by construction:

- **Denominators are pooled sums, not per-session percentage averages** —
  `total_tournament` and `total_panel_measured_admissible` are summed across
  the whole window, so the packet stays comparable even if universe size
  varies session to session. Defined once in `build_acceptance_packet`; the
  R1 decision must use these, not an ad hoc per-session jq calculation.
- **Headline = measured-only**: `headline.dropped_measured_rate` /
  `headline.added_measured_rate`, pooled over the fixed denominators above.
  This is the retirement-decision input.
- **Proxy is reported, never pooled into the headline**: `packet["proxy"]`
  carries `total_dropped_proxy` / `total_added_proxy` for visibility only.
- **Taxonomy version is enforced, not assumed**: `build_acceptance_packet`
  raises `TaxonomyVersionMismatchError` if any pooled record's
  `taxonomy_version` differs from the window's — re-collect under one
  taxonomy version rather than silently blending basis definitions.
- **Reason histograms are split**: `reason_histogram_measured` /
  `reason_histogram_proxy`, so the per-basis breakdown behind the headline
  delta is auditable before any cutover.

Rollback stance per R1: tournament kept read-only for one quarter after any
cutover.
