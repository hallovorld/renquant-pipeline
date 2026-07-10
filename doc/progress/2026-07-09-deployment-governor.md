# Deployment Governor kernel (D2–D4) — flag OFF

**Date:** 2026-07-09
**PR:** pipeline feat/deployment-governor
**Design:** orchestrator PR #443 — `doc/design/2026-07-09-deployment-governor-rfc.md`
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

## Tests

68 new (18 governor unit + 28 allocator unit + 22 integration); full suite
1404 passed / 7 skipped.

## Scope notes / deviations from RFC (explicit)

- BEAR defensive-sleeve sessions keep the legacy path (fixed-slot policy,
  not a Kelly decision).
- Pure de-risking sells (E* < E_current with no entry to fund) are NOT
  force-emitted — down-moves realize through post-cost-positive pairs and
  the untouched exit stack.
- Governor runs only when the selection chain reaches `SizeAndEmitTask`
  (a full book short-circuits at `PrepareSelectionTask`, as today).
- `TopUpHeldTask`/`TrimHeldTask` are not auto-disabled when the Governor is
  enabled — the D5 strategy config must not enable both stacks at once.
