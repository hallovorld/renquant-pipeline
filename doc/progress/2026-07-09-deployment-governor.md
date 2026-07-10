# Deployment Governor kernel (D2–D4) — flag OFF   (PR #179)

STATUS:    delivered
WHAT:      Implements the Deployment Governor's three pipeline-owned layers
           (L1 E*/L2 allocator/L3 execution) behind a top-level
           `deployment_governor` config block, default OFF and
           byte-identical to legacy behavior when off or malformed.
WHY/DIR:   Implements orchestrator#443's D2-D4 deliverables
           (`doc/design/2026-07-09-deployment-governor-rfc.md` §2.1/§2.2/§2.3
           is the contract) now that #443 has merged. Strategy-104#50
           (D5 PREPARE) already ships the config block with `enabled: false`
           in ALL THREE configs (including shadow) and all values labeled
           placeholders — S1 shadow arming is explicitly deferred to a
           separate future PR after D6 tuning produces a frozen config.
EVIDENCE:  n/a (flag-off byte-identical code change, not a model/data claim
           — see Tests below for the regression-pinning evidence)
NEXT:      D6 nested-selection tuning on the frozen tuning subset (per
           orchestrator#443 §1) to produce a calibrated config; then a
           separate dedicated PR flips `shadow.json`'s `enabled` flag for
           S1 shadow arming.

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

## Tests

72 new (18 governor unit + 28 allocator unit + 26 integration); full suite
1408 passed / 7 skipped.

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
