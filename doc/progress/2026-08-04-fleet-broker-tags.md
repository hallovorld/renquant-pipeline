# 2026-08-04 — GOAL-9 fleet broker tags registered AT BIRTH (orch#794 AC2)

Applies the #793 consumer-checklist lesson mechanically instead of by incident:
today the blend-mom lane crashed on its FIRST session because its tag was
never added to `ALLOWED_BROKERS` (pipeline#264, found by running). The three
GOAL-9 fleet lanes get their tags BEFORE any rail lands:

- `alpaca_shadow_blend_mom_fast` — F2: zblend(reversal + FAST momentum);
  buildable now (2 components), dormant until the first fast artifact
  (2026-08-08).
- `alpaca_shadow_blend_rb_mom` — F1: zblend(reversal-blend[prod+clf] + slow);
  needs the BlendPanelScorer N-generalization (N_COMPONENTS=2 hardcoded,
  measured on orch#794).
- `alpaca_shadow_blend_rb_fast` — F3: same, fast leg.

Tests: both state_paths copies accept all three; state/db files pairwise
disjoint across all seven shadow-family tags; the `_mom` prefix does not
swallow `_mom_fast` (suffix-confusion guard). 31 passed.
