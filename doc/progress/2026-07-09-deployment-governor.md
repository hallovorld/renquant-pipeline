# Deployment Governor kernel (D2–D4) — flag OFF   (PR #179)

STATUS:    in-progress
WHAT:      Implements the Deployment Governor's three pipeline-owned layers
           (L1 E*/L2 allocator/L3 execution) behind a top-level
           `deployment_governor` config block, default OFF and
           byte-identical to legacy behavior when off or malformed. This
           round closes three gaps Codex's post-#443-merge code review
           found (see "Post-merge code review" below) — code-level review
           was explicitly deferred until #443 merged.
WHY/DIR:   Implements orchestrator#443's D2-D4 deliverables
           (`doc/design/2026-07-09-deployment-governor-rfc.md` §2.1/§2.2/§2.3
           is the contract) now that #443 has merged. Strategy-104#50
           (D5 PREPARE) already ships the config block with `enabled: false`
           in ALL THREE configs (including shadow) and all values labeled
           placeholders — S1 shadow arming is explicitly deferred to a
           separate future PR after D6 tuning produces a frozen config.
EVIDENCE:  n/a (flag-off byte-identical code change, not a model/data claim
           — see Tests below for the regression-pinning evidence)
NEXT:      Codex code-level review of this round's fixes; then D6
           nested-selection tuning on the frozen tuning subset (per
           orchestrator#443 §1) to produce a calibrated config; then a
           separate dedicated PR flips `shadow.json`'s `enabled` flag for
           S1 shadow arming.

**PR:** pipeline feat/deployment-governor
**Design:** orchestrator PR #443 (merged) — `doc/design/2026-07-09-deployment-governor-rfc.md`
(§2.1/§2.2/§2.3 are the contract) + `doc/design/2026-07-09-governor-prereg-replay-protocol.md`.

## What

Implements the Deployment Governor's three pipeline-owned layers behind the
top-level `deployment_governor` config block (default OFF — inert until the
strategy-104 D5 config PR defines it):

| Layer | Module | Contract |
|---|---|---|
| L1 Governor | `kernel/deployment_governor.py` | pure E* = min(Σ min(raw_i, cap_i), E_ceil(regime)); hysteresis band; confidence-scaled step limit; NO exposure floor; fail-closed `None` on model fault |
| L2 Allocator | `kernel/deployment_allocator.py` | RFC §2.2 EXACT down-only operator: min(raw,cap) top-k → sector/corr/no-buy projections (lowest conviction trimmed first, never raising) → E*/Σw scale-down → exact residual accounting; asserted cap invariant |
| L3 Execution | `kernel/pipeline/governor_sizing.py` + `SizeAndEmitTask` branch | greedy whole-share rounding in conviction order + residual-cash re-offer pass (generalized S6 A-3 deferred rescue); exit legs from weight deltas charged `tax_drag()` + linear cost, pair emitted only if post-cost positive; min-hold/§1091 no-sell masks |

## Flag-off contract

`deployment_governor` absent / `enabled: false` / malformed ⇒ BYTE-IDENTICAL
`SizeAndEmitTask` behaviour (orders, block reasons, counters) — pinned by
`tests/test_governor_sizing_integration.py` with the same off-vs-on sweep
discipline as `tests/test_one_share_floor_initiation.py`.

## Fail-closed semantics

Model fault (non-empty slate with zero usable μ̂/σ̂ moments), a held name
without a usable price, an unmapped regime, or invalid PV ⇒ the Governor
emits NO target and the legacy sizing path runs unchanged
(`governor_fault_fallback_legacy` counter + ledger fault stamp). A weak
slate is NOT a fault: low E* + slate stats (admitted count, Σraw, μ̂
dispersion) stamped for the decision ledger.

## Post-merge code review (2026-07-10, after orchestrator #443 merged)

Codex's #179 review had been blocked purely on sequencing + one safety
note, explicitly deferring code-level review ("I will review the
code-level invariants in this PR on its then-current head") until #443
merged. Two things independently verified, three gaps closed:

**Safety note verified, not just assumed**: Codex flagged "do not merge
while strategy-104 #50 contains placeholder values and shadow enabled
true". Checked #50's actual merged configs (`origin/main`,
`renquant-strategy-104`): `deployment_governor.enabled = false` in ALL
THREE configs (prod/golden/shadow) — #50's own comment records this was
already fixed per a prior Codex review on that PR specifically to prevent
"shadow=true with uncalibrated placeholders" from being a silent future
arming. The risk is not live; confirmed by reading the config, not assumed.

**Gap 1 — only one of three frozen L1 candidates was implemented.** RFC
§2.1 (r4 review) freezes three independent candidates —
(A) `E*_ceil = E_ceil(regime)`, (B) `E*_kelly = min(E_raw, E_ceil)`
(the only prior implementation), (C) `E*_voltarget = min(E_vol, E_ceil)`
— "implemented behind the same config surface", with D6 Phase-2's
confirmatory run picking the live default. Added an `l1_candidate`
selector (default `"kelly"`, preserving exact prior behavior including
the original strict-inequality `ceiling_bound` semantics). Candidate (C)
reuses the EXISTING R-02 SPY-proxied vol-target convention
(`kernel/vol_target.py::compute_vol_target_scale`, already
regime-overridable via `portfolio_qp/tasks.py::_resolve_regime_override`)
rather than inventing a new portfolio-covariance estimate this codebase
doesn't have — this is the "existing regime-vol-band table" §2.1 refers
to.

**Gap 2 — L2's residual didn't carry the 3-source taxonomy.** RFC §2.2's
corrected feasibility statement names three distinct reasons
`E_final < E*`: step-2 projections (`cap_sector`/`cap_corr`/`mask`),
`low_conviction` (E_raw itself below E*), `breadth_bound` (fewer than
top_k candidates reached this stage at all — a SELECT-stage fact,
checked against the raw candidate count, NOT the post-admission-filter
count, so it doesn't swallow ordinary weak-slate `low_conviction` days).
Added `e_raw` and `residual_reason` to `AllocationResult`, computed by
precedence (breadth → projection-binder → low_conviction) per the RFC's
own ordering.

**Gap 3 — no executed-state ledger fields.** RFC §2.3 requires
`E_executed = Σ(shares_i·p_i)/PV` and `integer_residual = E_final −
E_executed` stamped per session, distinct from L1's `e_target` and L2's
continuous `e_final`/`residual`. Neither existed in `_stamp_ledger`.
Added both, computed from the actual post-fill/post-sell realized
weights (tracking `sold_shares` through the pair-sell loop, which wasn't
previously persisted past the local sell computation).

**Verified, no code change needed — "conviction ranking"**: traced every
use of `raw_i` across L1/L2/L3 (top-k selection, trim-priority ordering,
greedy buy/sell fill order) — it sets a weight exactly ONCE via
`min(raw_i, cap_i)` and is reused only as a monotone ordering key
elsewhere, never as a second multiplicative factor (the retired
conviction×sigma double-count bug). Added two explicit regression tests
pinning this rather than leaving it implicit.

## Tests

72 original (18 governor unit + 28 allocator unit + 26 integration) + 17
new this round (7 L1-candidate + 10 allocator residual-reason/conviction)
= 89 in the touched files; full repo suite 1425 passed / 7 skipped (was
1408/7 before this round — +17, zero regressions, zero new failures).

## Scope notes / deviations from RFC (explicit)

- BEAR defensive-sleeve sessions keep the legacy path (fixed-slot policy,
  not a Kelly decision).
- Pure de-risking sells (E* < E_current with no entry to fund) are NOT
  force-emitted — down-moves realize through post-cost-positive pairs and
  the untouched exit stack.
- Governor runs only when the selection chain reaches `SizeAndEmitTask`
  (a full book short-circuits at `PrepareSelectionTask`, as today).
- `TopUpHeldTask`/`TrimHeldTask` ownership: RESOLVED STRUCTURALLY
  (follow-up commit). When the Governor actually ran (flag on AND no
  fault fallback), `run_governor_sizing` stamps
  `ctx._governor_owns_sizing`; TopUp/Trim then NO-OP with ledger counters
  (`topup_suppressed_governor_owns_sizing` /
  `trim_suppressed_governor_owns_sizing`) — the Governor owns ALL sizing
  when active, so a live top-up would double-add to positions and pollute
  S1 shadow data. Fault-fallback sessions never set the flag — legacy
  top-up/trim stay fully ACTIVE; flag-off remains byte-identical (the
  attribute never exists).
