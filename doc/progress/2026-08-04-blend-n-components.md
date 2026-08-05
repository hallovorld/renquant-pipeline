# 2026-08-04 — BlendPanelScorer generalizes to N ≥ 2 components (GOAL-9 AC3)

Per the decision recorded on orch#794 (2026-08-04): the combination rule is an
unweighted sum of per-component cross-sectional z-scores and the scoring loop
was already N-ready — the certified 2-component construction generalizes
VERBATIM. This PR changes exactly two count checks:

- ctor: `!= N_COMPONENTS(2)` → `< MIN_COMPONENTS(2)` ("at least 2")
- `load_blend_scorer` config check: same generalization

Per-component WEIGHTS are deliberately NOT introduced — weighting is the MoE
stage's own preregistered change (orch#794 AC5). The composite fingerprint
recipe was already order-bearing and N-ready (joins component fps with \n in
config order) and is unchanged.

Tests: count-floor cases (0/1 refuse with the new message); a NEW N=3 case —
three components load, score finitely on the literal matrix, and the composite
fp covers all three legs in order (duplicated leg proves order-bearing).
Blend suite 43 passed; blend+momentum+shadow-arm selection 131 passed.

Unblocks: F1 (revblend+slow) and F3 (revblend+fast) profiles (s104, next).
