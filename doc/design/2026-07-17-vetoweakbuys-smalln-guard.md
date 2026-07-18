# Design: VetoWeakBuys small-n guard (buy-admission floor, GOAL-5 adjacent)

Date: 2026-07-17
Status: RFC — design review required before any implementation
Owner: drafted personally per design-review policy
Evidence: renquant-orchestrator PR #543
(`doc/research/2026-07-17-vetoweakbuys-smalln-analysis.md`)

## 1. Problem

`VetoWeakBuysTask` (`kernel/panel_pipeline/job_panel_scoring.py`) in
`adaptive_mean_std` mode computes the buy-admission floor as
`max(buy_floor_min, mean + buy_floor_std_mult * sample_std)` over the
CURRENT scan's candidate rank_scores. The threshold is a self-referential
statistic: at small n it stops expressing "is this candidate good" and
expresses "is this candidate an outlier within its own tiny set".

Measured consequence (evidence memo §2–§3, re-derived independently from
the live DB before this draft):

| session    | n | floor (mean+1σ) | max score    | vetoed |
|------------|---|-----------------|--------------|--------|
| 2026-07-16 | 5 | 0.561           | 0.557 (ATI)  | 5/5    |
| 2026-07-17 | 5 | 0.577           | 0.564 (BWXT) | 5/5    |

Both governed-override sessions produced n=5 scans whose bimodal shape
(3 stocks ~0.53–0.56 + 2 sector ETFs ~0.45) inflates σ until the floor
exceeds the MAXIMUM candidate score — an all-veto by construction, not a
quality verdict. It also inverts ranking consistency: vetoed ATI (0.557,
μ=+0.025) outscored held GRMN (0.549). Monte Carlo puts P(all-veto) at
~20–22% for n=5 even iid, ~3% at n=10, ~0 at n≥15. Small scans are the
steady state while the diagnostic-only override (pipeline#203) is active,
so recurrence is structural until fixed.

The era-wide counterfactual at normal n (16–18 sessions, top-3 vetoed vs
admitted, session-paired bootstrap) is NULL at every horizon — there is no
evidence the statistical floor misbehaves at normal scan sizes. The fix
must therefore be surgical to the small-n branch and leave normal-n
behavior bit-identical.

`adaptive_quantile` mode has the same disease in a milder form: at n=5 a
q0.80 floor admits exactly one name — breadth is dictated by n, not edge.

## 2. Design

### 2.1 Config surface (strategy-owned, pipeline-implemented)

Two new OPTIONAL keys under `ranking.panel_scoring`, honored by BOTH
adaptive modes (`adaptive_mean_std`, `adaptive_mean_std_cap`,
`adaptive_quantile`):

- `buy_floor_min_n` (int) — minimum scan size for the self-referential
  statistic to be trusted. Proposed production value: **10**
  (P(all-veto) ≤ ~3% per the evidence memo's Monte Carlo).
- `buy_floor_absolute_smalln` (float) — absolute calibrated-rank floor
  used when `n < buy_floor_min_n`. Proposed production value: **0.50**
  (the calibrated better-than-even point — an external reference that
  cannot self-destruct at small n).

Floor computation becomes:

```
scores = finite rank_scores of current candidates; n = len(scores)
if n >= buy_floor_min_n:
    floor = <existing mode formula, UNCHANGED>
else:
    floor = max(buy_floor_min, buy_floor_absolute_smalln)
    floor_label = f"smalln-absolute(n={n} < N0={N0}) = {floor:.3f}"
```

Applied to 2026-07-16: admits ATI/EME/BWXT (0.533–0.557), still vetoes
XLI/XLY (0.449). Admitted names then face the UNCHANGED downstream gates
(conviction μ floor, Kelly min-edge, QP admission, correlation/sector
caps). This widens exactly one gate's degenerate branch; it bypasses
nothing.

### 2.2 Fail-closed shape (AC6)

- BOTH keys absent → status quo (existing formulas, bit-identical),
  which fails toward no-entry. Rollout requires an explicit
  strategy-config change in renquant-strategy-104, reviewed separately.
- `buy_floor_min_n` set but `buy_floor_absolute_smalln` absent (or
  non-finite / outside (0, 1)) → config REJECTED at task start with a
  loud log line, floor falls back to status quo. A half-configured guard
  must not invent a default admission threshold.
- No runtime exception path: no env-var, no operator bypass. The
  existing `RQ_SIM_BYPASS_BUY_FLOOR` sim-only escape is untouched and
  remains sim-gated.
- NaN handling (`veto:rank_score_nan`) unchanged; the n counted for the
  guard is the FINITE-score count, consistent with the statistic it
  replaces.

### 2.3 Detection surface

- The existing veto log line already emits n and the floor formula;
  the new branch's `floor_label` makes small-n activation grep-able
  (`smalln-absolute`).
- `FunnelIntegrityTask` already tags `single_gate_funnel_kill`
  (verified `funnel_integrity_structural=1` on both override sessions) —
  unchanged.
- Orchestrator side (separate mechanical PR, not this repo): one
  degradation-sentinel LOUD rule — `all-vetoed AND n < N0` — so any
  recurrence pages instead of reading as a quiet no-trade day. The
  sentinel reads N0 from the pinned strategy config; if the guard is
  ever deconfigured the rule keeps firing on small-n all-vetoes, which
  is exactly the desired alarm.

### 2.4 What this design does NOT do

- Does not change normal-n behavior (evidence memo §4 NULL — no measured
  justification; any global floor redesign belongs to the G1
  equal-weight / breadth research track).
- Does not recover May-type score-scale-collapse all-vetoes (max score
  0.26 « 0.50): that failure class needs a calibrator-output scale
  integrity check — tracked separately, out of scope here.
- Does not touch admission semantics downstream of this task, the
  governed override (pipeline#203), or the WF gate.

## 3. Implementation plan (post-approval)

1. `VetoWeakBuysTask` (kernel `job_panel_scoring.py` + the
   `panel_scoring.py` twin, kept in lockstep): guard extraction +
   validation helper, applied to the three adaptive modes.
2. Tests: (a) n≥N0 → bit-identical floors vs current implementation on
   recorded 07-10 (n=85) scores; (b) n<N0 → absolute floor, 07-16/07-17
   recorded scores admit ATI/EME/BWXT and veto XLI/XLY; (c) half-config
   rejection → status-quo floor + loud log; (d) NaN candidates excluded
   from n; (e) quantile mode small-n branch.
3. Strategy-config PR in renquant-strategy-104 setting
   `buy_floor_min_n: 10`, `buy_floor_absolute_smalln: 0.50` — separate
   review, separate merge; nothing activates until it lands and pins
   bump through the normal path.
4. Orchestrator sentinel-rule PR (§2.3).
5. Shadow verification before live: one session with the guard active in
   the shadow arm, confirming funnel counts and that downstream gates
   (not the floor) decide the small-n outcome.

## 4. Acceptance criteria (measurable)

- AC-a: with the production config values, replaying the recorded
  2026-07-16 and 2026-07-17 candidate sets admits exactly
  {ATI, EME, BWXT} past the floor and vetoes {XLI, XLY}.
- AC-b: replaying every recorded n≥N0 live session since 2026-04-22
  yields floors identical to the current implementation (no normal-n
  drift).
- AC-c: config-absent replay of the same sessions is bit-identical to
  today (fail-closed proof).
- AC-d: the sentinel rule fires on a synthetic all-vetoed n<N0 day and
  stays quiet on a normal-n partial-veto day.
