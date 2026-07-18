# Design: VetoWeakBuys small-n guard (buy-admission floor, GOAL-5 adjacent)

Date: 2026-07-17
Status: RFC — design review required before any implementation
Owner: drafted personally per design-review policy
Evidence: renquant-orchestrator PR #543
(`doc/research/2026-07-17-vetoweakbuys-smalln-analysis.md`) + the r2
evidence pack (empirical-mixture Monte Carlo, threshold return-split,
one-sidedness replay) — reproducible script and a correction note to the
#543 memo's "essentially deterministic" phrasing land in a follow-up
orchestrator PR alongside this round.

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

Both governed-override sessions produced n=5 scans where the floor
exceeded the MAXIMUM candidate score — an all-veto by construction, not
a quality verdict. It also inverts ranking consistency: vetoed ATI
(0.557, μ=+0.025) outscored held GRMN (0.549).

Corrected probability model (r2 — replaces the evidence memo's
"essentially deterministic" phrasing, which overstated): a Monte Carlo
fitted to the ACTUAL post-06-01 score mixture (stocks mean 0.518 σ 0.050,
sector ETFs mean 0.481 σ 0.046 — the ETF cluster sits only ~0.7σ below
stocks, so its σ-inflation effect is modest) puts P(all-veto) ≈ 0.17–0.20
at n=5, ≈ 0.02 at n=10, ≤ 0.01 at n=12, under iid, mixture, and
bootstrap-from-empirical fits alike. The realized 2-for-2 is a ~1-in-5
per-session event repeating on consecutive structurally-similar scans —
high-frequency, not deterministic. Small scans are the steady state while
the diagnostic-only override (pipeline#203) is active, so a ~20%/session
all-veto rate compounds fast: recurrence is structural until fixed.

The era-wide counterfactual at normal n (16–18 sessions, top-3 vetoed vs
admitted, session-paired bootstrap) is NULL at every horizon — there is no
evidence the statistical floor misbehaves at normal scan sizes. The fix
must therefore be surgical to the small-n branch and leave normal-n
behavior bit-identical.

`adaptive_quantile` mode has the same disease in a milder form: at n=5 a
q0.80 floor admits exactly one name — breadth is dictated by n, not edge.

## 2. Design

### 2.1 Config surface (strategy-owned, pipeline-implemented)

Two new OPTIONAL keys under `ranking.panel_scoring`, honored by ALL
three adaptive modes (`adaptive_mean_std`, `adaptive_mean_std_cap`,
`adaptive_quantile`):

- `buy_floor_min_n` (int) — minimum scan size for the self-referential
  statistic to be trusted on its own. Proposed production value: **12**
  — the smallest n where P(all-veto) ≤ 1% under BOTH the
  empirical-mixture normal fit and the bootstrap-from-empirical fit
  (r2 Monte Carlo; n=10 leaves ~2%, n=15 buys only ~0.2% more).
- `buy_floor_absolute_smalln` (float) — the small-n relaxation bound.
  Proposed production value: **0.50** — a DISTRIBUTIONAL ANCHOR (the
  post-scale-fix cross-sectional median is ≈ 0.515, so 0.50 sits just
  below the typical mid-cross-section), NOT a claim of calibrated
  better-than-even probability; see §2.4 for why that earlier claim is
  withdrawn.

**Floor computation — RELAX-ONLY (r2, replaces the r1 hard-switch):**

```
scores = finite rank_scores of current candidates; n = len(scores)
F_mode = <existing mode formula, UNCHANGED>          # incl. its max(buy_floor_min, ·)
if n >= buy_floor_min_n:
    floor = F_mode                                   # bit-identical to today
else:
    floor = max(buy_floor_min, min(F_mode, buy_floor_absolute_smalln))
    floor_label = f"smalln-relax(n={n} < N0, min(mode={F_mode:.3f}, abs={ABS:.2f})) = {floor:.3f}"
```

The `min()` makes the small-n branch ONE-SIDED by construction: it can
only LOWER the floor (widen admission) relative to the status quo, never
raise it. This kills the r1 design's scale-stability failure — on a
compressed or collapsed score scale (April-era max ≈ 0.26, or a
Platt-compressed range ~0.07 centered below 0.50) the r1 hard-switch to
an absolute 0.50 would have INVENTED all-vetoes the current rule doesn't
have; the relax-only form degrades exactly to status quo there. Verified
on all 14 recorded small-n/all-veto-adjacent sessions plus two synthetic
compressed sets: admitted count under the new rule ≥ status quo in every
case, with 07-16/07-17 going 0 → 3 admitted (ATI/EME/BWXT; XLI/XLY still
vetoed at 0.449 < 0.50). Admitted names then face the UNCHANGED
downstream gates (conviction μ floor, Kelly min-edge, QP admission,
correlation/sector caps). This widens exactly one gate's degenerate
branch; it bypasses nothing.

### 2.2 Fail-closed shape (AC6) — complete config matrix (r2)

| `buy_floor_min_n` | `buy_floor_absolute_smalln` | Behavior |
|---|---|---|
| absent | absent | status quo, bit-identical (guard fully off) |
| valid | valid | guard active per §2.1 |
| valid | absent / invalid | config REJECTED loudly → status quo |
| absent / invalid | valid | config REJECTED loudly → status quo |

- **Validation bounds (r2):** `buy_floor_min_n` must be an integer in
  **[2, 30]**; `buy_floor_absolute_smalln` must be finite and in
  **(0, 1)**. Anything else is REJECTED (loud log + status quo). The
  [2, 30] bound stops a typo like `100` from silently activating the
  small-n branch at every real scan size — and even if a wrong-large N0
  ever slipped through, the §2.1 relax-only `min()` bounds the damage:
  the branch can only widen admission relative to the statistical floor,
  never narrow it (defense in depth, not a substitute for the bound).
- Rejection is per-run, logged at ERROR with the offending key and
  value, and falls back to the status-quo formula — which fails toward
  no-entry. Rollout requires an explicit strategy-config change in
  renquant-strategy-104, reviewed separately.
- No runtime exception path: no NEW env-var, no operator bypass. The
  ONE existing env influence on this task, `RQ_SIM_BYPASS_BUY_FLOOR`, is
  untouched and remains hard-gated to `run_type == "sim"` inside the
  task (prod/live/cron runs log a warning and keep the floor active even
  if the env leaks) — stated here so the "no exception path" claim and
  that pre-existing sim escape are reconciled explicitly.
- NaN handling (`veto:rank_score_nan`) unchanged; the n counted for the
  guard is the FINITE-score count, consistent with the statistic it
  replaces.

### 2.3 Detection surface

- The existing veto log line already emits n and the floor formula;
  the new branch's `floor_label` makes small-n activation grep-able
  (`smalln-relax`).
- `FunnelIntegrityTask` already tags `single_gate_funnel_kill`
  (verified `funnel_integrity_structural=1` on both override sessions) —
  unchanged.
- Orchestrator side (separate mechanical PR, not this repo): one
  degradation-sentinel LOUD rule — `all-vetoed AND n < N0_sentinel` —
  so any recurrence pages instead of reading as a quiet no-trade day.
  **N0_sentinel (r2):** the sentinel uses
  `max(12, config buy_floor_min_n if valid else 0)` — it does NOT
  depend on the guard being configured. If the guard is absent,
  rejected, or deconfigured, the sentinel still fires on any small-n
  all-veto at its own built-in 12; the alarm can never go quiet exactly
  when the guard is broken (closing the r1 review's half-config
  blind spot).

### 2.4 Threshold semantics — what 0.50 is and is not (r2, honest)

The r1 draft called 0.50 "the calibrated better-than-even point". That
claim is WITHDRAWN — the r2 evidence run shows it is not supportable:

- **Scale stability:** fraction of sessions whose MAX score sat below
  0.50 — 2026-04: 0.80, 2026-05: 0.19, 2026-06: 0.00, 2026-07: 0.00.
  The scale holds in the current era but has moved under 0.50 twice
  this year. (The relax-only construction is what makes this survivable:
  on such scales the branch degrades to status quo instead of
  all-vetoing.)
- **Return separation:** the post-scale-fix ≥0.50 vs <0.50 forward
  excess-return split is noise at h=1/h=5 (CIs straddle zero) and
  significantly NEGATIVE at h=20 (−0.98% [−1.53, −0.44], overlapping
  windows so the CI is understated). Sensitivity at 0.45/0.55 flips
  signs across horizons. 0.50 does not separate winners from losers on
  this calibrator's output.

What 0.50 IS: a distributional anchor — the post-fix cross-sectional
median is ≈ 0.515, so 0.50 admits roughly the top half of a typical
scan when the statistical floor degenerates, instead of the top ~sixth
(mean+1σ) or nobody. The small-n branch is a breadth backstop, not an
alpha filter; alpha discrimination remains the job of the downstream μ,
Kelly, and QP gates. Any claim stronger than that must come from the G1
equal-weight / breadth research track, not this gate.

### 2.5 What this design does NOT do

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
   recorded 07-10 (n=85) scores; (b) n<N0 → relax-only floor,
   07-16/07-17 recorded scores admit ATI/EME/BWXT and veto XLI/XLY;
   (c) BOTH half-config directions + out-of-bounds values (min_n=1,
   min_n=100, absolute=0, absolute=1.2, absolute=NaN) → rejection,
   status-quo floor, ERROR log; (d) NaN candidates excluded from n;
   (e) all three adaptive modes' small-n branches (mean_std, cap
   variant, quantile); (f) ONE-SIDEDNESS: replaying every recorded
   small-n/all-veto-adjacent session (14) plus the two synthetic
   Platt-compressed sets (range 0.07 centered 0.45 / 0.55), admitted
   count under the guard ≥ status quo in every case.
3. Strategy-config PR in renquant-strategy-104 setting
   `buy_floor_min_n: 12`, `buy_floor_absolute_smalln: 0.50` — separate
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
- AC-b: replaying ALL 23 veto-active live sessions recorded since
  2026-04-22 (the enumerated set in orchestrator #543 §2 — not "all
  available", the named list, so the AC cannot pass vacuously): every
  session with n≥N0 yields a floor identical to the current
  implementation.
- AC-c: config-absent replay of the same 23 sessions is bit-identical
  to today (fail-closed proof), and each invalid-config case from §2.2
  degrades to the same bit-identical floors.
- AC-d: the sentinel rule fires on a synthetic all-vetoed n<N0 day,
  fires with the guard DECONFIGURED (built-in N0_sentinel=12), and
  stays quiet on a normal-n partial-veto day.
- AC-e: one-sidedness — across the 14 recorded small-n sessions and
  both synthetic compressed sets, admitted count with the guard is ≥
  the status-quo count in every single case (zero violations).
