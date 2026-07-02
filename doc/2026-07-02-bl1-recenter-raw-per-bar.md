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
