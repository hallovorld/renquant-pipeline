# Rotation: a blocked pair must try the next sell leg / next candidate, not give up

Date: 2026-08-25
Branch: `fix/rotation-next-candidate`
Operator directive: "rotate失败就是bug" — the 2026-08-25 session's give-up behavior
is a defect to fix, not a policy to explain.

## 1. The incident (measured)

Live session 2026-08-25, regime BULL_CALM, held=7, eq≈$10,825:

- `find_rotation_pairs` proposed exactly one pair: **sell LLY → buy CRWD**.
- `ValidatePairsTask` killed it: `correlation_guard` — corr(CRWD, PANW) =
  **0.845** [VERIFIED: `backtesting/renquant_104/artifacts/prod/watchlist-correlation.json`]
  against threshold 0.70, with PANW still in `virtual_held` because the pair
  sells LLY, not PANW.
- The engine then did **nothing**: no attempt to pair CRWD with a different
  sell leg (selling PANW would have removed the very conflict), and no attempt
  to rotate into the next-ranked candidate. The day's only rotation slot was
  burned by a pairing the validator was always going to refuse.

This is the same defect class pipeline#289 fixed for the long-signal guard
(a post-pairing veto that the pairing loop cannot see), now measured on the
remaining three guards: wash-sale, sector, correlation.

## 2. The fix

`find_rotation_pairs` gains an injectable `buy_leg_admissible(buy, sell|None)`
predicate, built in `task_rotation.py` from the *same* kernel guards the
validator runs, with the validator's own `virtual_held = held − sell_leg`
semantics:

- `sell=None` → candidate-level check (wash-sale incl. the #223 materiality
  floor): an untradeable buy leg skips to the **next candidate** before any
  pairing is consumed.
- `sell=t` → pair-level check (sector guard + correlation guard on
  `held − {t}`): a refused pairing tries the **next sell leg**; only when every
  viable sell leg is exhausted is the candidate recorded blocked
  (stage="prefilter", `rotation_<reason>` counters — #289 conventions exactly).

`ValidatePairsTask` is unchanged and remains the authority (defence in depth);
the prefilter only predicts it, argument-for-argument at each guard call site.

## 3. §4(b) evidence

- **Replay of the incident** (`test_replay_20260825_corr_blocked_candidate_yields_to_the_next`,
  real corr values incl. the measured 0.845): the fixed engine emits
  **(sell PANW → buy CRWD)** — it finds the sell leg that *resolves* the
  conflict, upgrading the correlated slot in place (ER +0.02 → +0.0995), and
  `ValidatePairsTask` confirms the pair. Strictly better than "skip to the
  next candidate": the top candidate still gets bought. [VERIFIED: test run]
- **No over-blocking**: a candidate correlated only with the sell leg itself
  still pairs (the virtual-held subtlety, asserted). [VERIFIED]
- **Wash-sale yield**: a wash-sale-blocked top candidate cedes to the runner-up
  without consuming the pairing. [VERIFIED]
- **Counter semantics**: failing only the ER-advantage threshold is *not*
  recorded as blocked (no guard fired). [VERIFIED]
- Full pipeline suite: **2669 passed, 9 skipped, 0 failed** under the
  CI-matching uv env. [VERIFIED: local run 2026-08-25]

## 3b. Review r2 — cross-pair statefulness + validator-order equivalence

Codex (P1, correct): the r1 prefilter closed over the OPENING book, so it was
equivalent to `ValidatePairsTask` only for the first pair — the validator's
`virtual_held` is stateful across pairs (`held − validated sells − this sell
+ validated buys`). For pair 2+, the prefilter could miss a conflict pair 1
introduced or reject against a holding pair 1 already sold — recreating the
post-pairing give-up.

Rework, two mechanisms:

1. **Stateful walk**: the callback is now
   `(buy, sell|None, accepted=<tuple of RotationPair>)`; the kernel passes
   the tentatively accepted pairs, and `_build_buy_leg_admissible` evaluates
   sector/correlation against the validator's exact expression
   `set(held) − accepted sells − this sell | accepted buys`.
2. **Validator-order simulation** (a gap codex's text implied but did not
   name): the validator walks the **margin-sorted** list, while pairs are
   accepted in candidate-rank order — with 2+ pairs the orders can differ,
   and walk-order admissibility does not imply sorted-order admissibility in
   either direction. After sorting, the kernel simulates the validator's
   stateful loop in the validator's exact order; an offender is vetoed and
   the walk re-runs without that (sell, buy) combination — retry, never
   give-up. Terminates: each iteration permanently vetoes one combination
   from a finite set. Blocked-candidate records fire once, from the walk
   actually returned.

Evidence: three new tests — pair-1-buy-conflicts-pair-2-buy (next candidate
tried, validator preserves all emitted), pair-1-sell-unblocks-pair-2 (the
inverse codex asked for), and an acceptance-vs-validator-order divergence
that only the simulation catches. Mutation sanity [VERIFIED]: freezing the
prefilter on original holdings fails exactly the two statefulness tests;
disabling the simulation fails exactly the order test; restored, all pass.

## 4. Also in this PR (separate commits)

- `test_wash_sale_cost_branch_reachability.py`: call-site census 6 → 7 — the
  guard test doing its job; the 7th site is the prefilter mirroring the
  validator site, still passing no `expected_dollar_return` (R7 finding
  unchanged).
- `test_wf_fail_override.py::test_wrong_scorer_authorization_blocks`: pinned
  fixture expiry `2026-08-24` met the real clock today and the test started
  asserting the wrong rejection ("expired" beat "scorer_mismatch"). Expiry is
  now clock-relative, same pattern as the neighboring valid-path test. This
  failure predates the branch and would have turned CI red for any PR today.

## 5. Deploy note

Merge ≠ live: the daily runner consumes the **pinned** pipeline via the
umbrella `.subrepo_runtime`. After merge this needs a pin advance + runtime
sync (ask-first, with preflight) before the next session benefits.
