# BL-1 / M4: per-bar raw recentering before the global calibrator (flag, default OFF)

Plan: renquant-orchestrator `doc/design/2026-07-02-unified-107-master-plan.md`,
Term IC row **M4** — "recenter raw per bar; BL-4 stays interim guard; shadow
replay first".

## Live evidence (2026-07-01/02 production)

```
GlobalPanelCalibration.load: ER=0 neutral sits at raw=-0.2902 (not 0) ... raw
scores in (-0.2902, 0.0000) map to a μ of the OPPOSITE sign to their raw
signal. Signal-direction gating must use this anchor, not a hard-coded 0.
```

`calibrator_sign_laundered` = **44** (2026-07-01) and **45** (2026-07-02) of
~90 candidates. The BL-4 signal-direction gate
(`kernel/pipeline/signal_direction.py`) is the interim guard and stays in
force — this change does not touch it.

## Mechanism

The pooled calibrator is fit over history, so its ER=0 neutral
(`neutral_raw` ≈ −0.2902 live) encodes the TRAINING cross-sections' center.
Live cross-sections drift (the June scorer's per-bar median sits at ≈ −0.05,
0.24 above the anchor), so every raw score between the live center and the
stale anchor maps to a μ of the opposite sign to its cross-sectional stance.

Fix, under `ranking.panel_scoring.global_calibration.recenter_raw_per_bar`
(default **false**; flag absent ⇒ byte-identical legacy path):

```
calibrator_input = raw − median(per-bar cross-section) + neutral_raw
```

- The calibrator's neutral then coincides with the actual per-bar center **by
  construction**: above-center → μ>0, below-center → μ<0.
- **Median**, not mean: raw ranker tails are heavy; one blown-out score would
  drag a mean-center and flip near-center μ signs. The median is invariant to
  tail magnitudes and matches the rank-centric meaning of "center".
- Center from `ctx.candidates` (the full scored panel at apply time, pre-veto);
  holdings are shifted with the SAME value so rotation comparisons stay on one
  scale. `c.panel_score` is **never mutated** — only the interpolation-head
  input — so `raw_panel` persistence, decision traces, and the BL-4 raw gate
  are untouched.
- With the flag ON, the BL-2 `calibrator_sign_laundered` counter tests the
  RECENTERED sign vs μ sign (the M4 acceptance metric); OFF keeps the legacy
  raw-sign test bit-for-bit.
- Safe fallbacks (raw path, warning): no ER=0 anchor; < 3 finite scores.

## Shadow replay (read-only, committed evidence)

`scripts/shadow_replay_bl1_recenter.py` replayed the last 6 FULL live runs'
stored `score_distribution.raw_panel` through the live prod calibrator
(`panel-rank-calibration.json`, sha in the JSON). Evidence:
`doc/evidence/2026-07-02-bl1-recenter-shadow-replay.json`.

| date | n | center | laundered before → after | admitted @0.03 before → after |
|---|---|---|---|---|
| 2026-07-02 | 83 | −0.0362 | **45 → 0** | 22 → 1 |
| 2026-07-01 | 83 | −0.0529 | **44 → 0** | 17 → 1 |
| 2026-06-30 | 83 | −0.0464 | 43 → 0 | 18 → 1 |
| 2026-06-26 | 79 | −0.0473 | 46 → 0 | 18 → 0 |
| 2026-06-25 | 76 | −0.2973 | 26 → 0 | 5 → 6 |
| 2026-06-24 | 73 | −0.2817 | 23 → 0 | 3 → 3 |

- Replay fidelity: before-μ reproduces the stored prod μ exactly (max |Δ| =
  0.0) on 07-01/02 — and reproduces the LIVE logged counters 44/45 exactly.
  Earlier days show |Δ| ≤ 0.0035 (weekly calibrator vintage drift, surfaced
  deliberately by the fidelity check).
- Acceptance metric: laundered → **0** on all six runs (target: single
  digits).
- μ distribution: the ~+0.019 unconditional intercept is removed; median μ →
  0 by construction (07-02: mean +0.0189 → −0.0024, p10/p90 +0.0009/+0.0343 →
  −0.0207/+0.0129).

## Interaction warning — the enable path is a strategy-104 decision

Recentering changes μ's ABSOLUTE level. Per the M4 contract this PR leaves
every downstream threshold untouched, and the replay shows the consequence:
the absolute `conviction_gate.mu_floor=0.03` admits **~0–1** names
post-recentering on the drifted June cross-sections (22→1, 17→1, 18→1, 18→0),
because that floor was mostly gating the +2-3% drift intercept, not
conviction (on 06-24/25, where the live center ≈ anchor, the delta is ~nil).
The plan row's "admission delta = laundered names only" does NOT hold at the
raw floor — measured and reported honestly here.

Therefore: enabling `recenter_raw_per_bar` ALONE with today's floor is
near-sell-only — the same failure algebra as the 2026-06-29 demean revert.
Enable path: shadow verdict → a strategy-104 config PR that flips the flag
AND re-derives the floor as a relative-conviction quantity (operator
decision), never a silent default-ON here.

## Frozen enable protocol (codex review, 2026-07-02)

This is a coupled recenter+threshold redesign, not an isolated hygiene flag —
frozen here, before any enable PR, so a future decision is measured against a
pre-committed bar rather than calibrated post-hoc against whatever the
holdout happens to show.

**Which threshold(s) re-derive.** `ranking.panel_scoring.conviction_gate
.mu_floor` is the only absolute economic threshold this repo applies to
`expected_return`/μ (verified: no other `expected_return`-keyed absolute
gate exists in `job_panel_scoring.py`). It is the one and only knob the
enable PR is allowed to touch. Re-derivation must follow the SAME pattern
already validated for the parallel drift problem in this file — the
`demean_cross_sectional` flag on `ConvictionGateTask` (2026-06-24, 20/20
live runs zero-buy-free) — i.e. the floor becomes a RELATIVE-conviction
quantity (demeaned or equivalent), never a new absolute number. A new
absolute constant just relocates today's failure to a different point on a
distribution that keeps drifting.

**Sample.** Re-derivation is computed on live runs STRICTLY AFTER the six
already inspected above (07-02, 07-01, 06-30, 06-26, 06-25, 06-24) — those
six diagnosed the problem and are disqualified as fitting data for its
fix, on the same logic as the WF-gate threshold-freeze fix elsewhere this
session (renquant-backtesting#61): a threshold tuned on the data used to
motivate it is not validated, it's calibrated to the answer. Minimum
sample: the next 10 FULL live runs (`min_candidates=60`, same definition
`shadow_replay_bl1_recenter.py` already uses) counted from the enable PR's
open date.

**Out-of-sample acceptance metrics** (all must hold on that holdout, via
`scripts/shadow_replay_bl1_recenter.py --mu-floor <re-derived>` or its
successor):
1. `calibrator_sign_laundered` (recentered sign vs μ sign) stays in single
   digits — necessary, not sufficient (already this PR's M4 metric).
2. Admitted-candidate count per run falls in a defensible band, not the
   0-1 collapse this PR's own replay measured (`22→1, 17→1, 18→1, 18→0`) —
   e.g. a floor that admits fewer than ~5 names on >2 of the 10 holdout
   runs is a fail, full stop, regardless of (1).
3. The admitted set's SUBSEQUENT realized forward-excess-return
   distribution (mean/median over the holdout) is not statistically worse
   than the legacy (pre-recenter, pre-refloor) admitted set's realized
   forward-excess-return over the SAME 10 runs — this is the metric that
   actually matters; (1) and (2) are necessary preconditions, not
   sufficient evidence of a net-positive change.

**Success is not "laundering went to zero."** A floor that admits ~0 names
also drives laundering to ~0 trivially (nothing is admitted to launder).
Success requires ALL THREE metrics above to hold together — laundering
near-zero AND a sane admitted count AND non-inferior realized returns.
Failing (2) or (3) while (1) passes means: revert to legacy (flag OFF),
do not ship.

**Still true from the original doc:** enabling `recenter_raw_per_bar`
without ALSO re-deriving `mu_floor` per this protocol remains near-sell-only
and out of scope for any PR that doesn't carry this evidence. This PR keeps
the flag default-OFF; it does not attempt re-derivation itself.
