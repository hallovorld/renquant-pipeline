# Progress: VetoWeakBuys small-n guard design RFC

Date: 2026-07-17

## What

Design RFC `doc/design/2026-07-17-vetoweakbuys-smalln-guard.md`: a
minimum-n guard for the self-referential buy-admission floor. When the
scan has fewer than `buy_floor_min_n` finite-scored candidates, the
adaptive (mean+σ / quantile) floor is replaced by an absolute calibrated
threshold `buy_floor_absolute_smalln`; at normal n behavior is
bit-identical to today.

## Why

Evidence memo (renquant-orchestrator #543): both governed-override
sessions (2026-07-16, 2026-07-17) scanned n=5 and the mean+1σ floor
exceeded the maximum candidate score — 5/5 vetoed by construction both
days, book frozen ~86% cash, with a ranking inversion (vetoed ATI 0.557
vs held GRMN 0.549). Era-wide counterfactual at normal n is NULL, so the
fix is surgical to the small-n branch only.

## Round 2 (codex review, 3 P1s — all fixed with fresh evidence)

- Small-n branch redesigned RELAX-ONLY: `max(min_fl, min(F_mode, abs))`
  — one-sided by construction (verified 0 violations on 14 recorded
  sessions + 2 synthetic compressed sets); r1's hard-switch would have
  invented all-vetoes on shifted scales.
- N0=12 justified by empirical-mixture MC (the iid≈mixture result also
  corrects the memo's "essentially deterministic" overstatement).
- "Calibrated better-than-even" claim WITHDRAWN; 0.50 reframed as a
  distributional anchor with the adverse h=20 split reported honestly.
- Complete config matrix with validation bounds; sentinel gets built-in
  N0_sentinel=12 independent of guard config.

## Status

RFC only — no implementation, no config change, no deployment. Rollout
(post-approval) is staged: pipeline implementation → strategy-104 config
PR → orchestrator sentinel rule → shadow verification → pins.
