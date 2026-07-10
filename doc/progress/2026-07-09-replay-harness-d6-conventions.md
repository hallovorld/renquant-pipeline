# Replay harness: opt-in D6 protocol conventions (stateful / tax / integer shares / in-arm caps)   (PR #180)

STATUS:    delivered
WHAT:      Adds D6 §1.1 frozen replay conventions (tax drag, whole-share
           quantization, stateful sessions, in-arm sector caps) to the
           allocator replay harness as strictly opt-in kwargs/CLI flags;
           defaults stay byte-for-byte identical to pre-change behavior.
WHY/DIR:   D6 preregistered replay protocol (orchestrator#443, merged) —
           the exploratory run documented in orchestrator#445 found the
           harness couldn't honor D6 §1.1's frozen conventions: (1) no tax
           drag, (2) no whole-share quantization, (3) stateless sessions
           (deployed fraction ≡ turnover; hysteresis unevaluable), (4)
           sector caps not enforced inside arms (35% gate breached by every
           arm silently at ~5.7 candidates/session breadth).
EVIDENCE:  n/a (harness/tooling change; see "Not implemented / caveats"
           below for the honest scope boundary against #443's final §2.3/§3
           text, and Tests below for regression-pinning evidence)
NEXT:      Codex re-review of the fail-closed sector-map check and the
           execution_fidelity/promotion_eligible stamping (see
           "Correction" below). After that: a D6 protocol runner (not yet
           built) to orchestrate the §3(a)/(b)/(c) estimand decomposition;
           the deferred one-share rescue + post-round cap/sector recheck belong in the live
           governor/allocator implementation (#179), not this harness.

## Correction (2026-07-10, same day — Codex review on the prior head)

Codex found two real gaps in the prior head:

1. **Sector-map coverage was silently permissive.** `--enforce-caps` with no
   `--sector-map-json` (or a map covering only SOME active tickers) only
   logged a warning; `apply_d6_cap_projection` then applied no sector
   constraint at all to the uncovered tickers. For a run that claims D6
   sector-cap fidelity, a missing hard constraint silently becoming no
   constraint is a correctness bug, not a documented limitation. **Fixed**:
   new `sector_map_coverage_gap()` (math module) scans every ticker across
   every replay bar; `run_ab_replay.py`'s CLI now FAILS CLOSED (writes an
   `invalid_experiment` artifact, exits 2) when the supplied map doesn't
   cover every active ticker, unless the new `--allow-partial-sector-map`
   escape hatch is passed (mirrors the existing
   `--allow-overlapping-forward-horizon` research-only pattern) — and even
   then the run is unconditionally stamped non-decision-grade (point 2).
2. **Result-laundering risk**: this doc already documented the harness as
   "L1/L2-only" in the caveats below, but the `## NEXT` section separately
   called the resulting runs "convention-faithful arms" without repeating
   that caveat — a reader of NEXT alone could mistake a floor-only result
   for decision-grade D6 end-to-end evidence. **Fixed**: every payload
   where ANY D6 convention is engaged now carries a machine-readable
   `execution_fidelity: "L1_L2_ONLY"` / `promotion_eligible: false` stamp
   (in both `constraint_fidelity` and `replay_conventions`), reusing the
   EXISTING `constraints_decision_grade` gate that already blocks
   promotion on missing hard-constraint families (`constraint_fidelity_
   block`, `apply_promotion_gate_to_significance`, `assemble_verdict`) —
   not a new parallel gate. `assemble_verdict` therefore always returns
   `promotion_candidate: None` with an explicit "not decision-grade"
   rationale whenever conventions are engaged, regardless of what the
   underlying significance/violation blocks would otherwise show. Default
   (no-conventions) evidence is unaffected — these keys are strictly
   additive, pinned by `TestDefaultModeUnchanged`.

**Cross-check on the #179 reference** (read-only — #179 is already merged,
not modified here): `pipeline#179`'s `governor_sizing.py` module docstring
and `_execute_deltas`'s residual pass DO implement a generalized one-share
deferred-rescue pattern (re-offering leftover cash one share at a time in
conviction order, S6 A-3-style) — so the "belongs in the live governor
implementation" pointer above is accurate, not just an assumption. Whether
`#179` also implements a full post-round cap/sector/correlation recheck ON
EXECUTED quantities was not independently re-verified here (out of this
PR's scope).

## What

All conventions are **strictly opt-in** (kwargs on `replay_one_allocator` /
`replay_all` / `run_replay` via a frozen `ReplayConventions` dataclass; CLI flags
on `run_ab_replay.py`). Defaults reproduce the pre-change behavior **byte-for-byte**
— pinned by a fixture evidence JSON generated on origin/main @ f6e818c before any
edit (`tests/fixtures/ab_replay_default_evidence.json` +
`tests/fixtures/d6_default_bars.py`), so all existing committed evidence stays
reproducible. The pin is two-tier: byte-identical on the platform that minted the
fixture (darwin), and exact-schema / integer-exact / float-ULP-tolerant (rel 1e-9)
everywhere else — CI's ubuntu numpy differs from the minting build in float
reduction order at the last ULP (observed in CI run 29071951222), which no
tolerance-free cross-platform pin can absorb.

### D6 §1.1 mapping

| D6 frozen convention | Harness implementation |
|---|---|
| Linear cost 5 bps/side on every traded dollar | already present (`cost_per_trade_bps=5.0` × L1 traded weight = per-side per traded dollar); new `--cost-bps` re-stamps loaded bars |
| Tax: realized-gain, short 50% / long 32%, lot holding period decides | `--tax` (stateful): FIFO lots, per-exit-leg `rotation.tax_drag()` convention (gain × rate; losses = zero drag); `--lt-threshold-days` 365 |
| Whole-share quantization in all arms | `--integer-shares`: `floor(w·PV/p)` per bar; executed-state invariant (round DOWN; post-round executed weights carry into state); per-session `E_executed` + `integer_residual` |
| Fill at session close price | share conversion anchors to the session `close_price` at (re-)entry; held positions are marked by the same per-bar `fwd_return` the stateless harness uses ("returns-consistent pricing" — see caveat) |
| §4 caps: name ≤ 12%, sector ≤ 35% | `--enforce-caps` + `--sector-map-json`: down-only projection INSIDE the arm before returns; per-session breach counters replace silent allowance |
| Deployed fraction as an estimand; hysteresis | `--stateful`: positions/lots/cash carried across sessions per arm; allocators receive the carried `w_current`; `deployed_fraction` series in evidence |

### Accounting conventions (stateful engine)

- **PV accounting:** `PV = cash + Σ lot market values`; session net return =
  `PV_close/PV_open − 1`; costs and taxes flow through cash → cash-conservation
  invariant is exact (tested to 1e-9 on a hand-computed 3-session chain).
- **Off-universe forced liquidation:** a carried position absent from the session
  universe is sold at carried value (zero-return exit, cost+tax charged) and
  counted (`off_universe_liquidations`) — keeps the budget the allocator sees exact.
- **Returns-consistent pricing (documented deviation):** internal prices anchor to
  the DB session close at entry, then evolve by `fwd_return`, so
  `shares × price ≡ market value` exactly and stateful/stateless arms are driven
  by the identical return series. Deviation from a pure close-to-close mark on
  non-contiguous sessions is a documented limitation, not silent.

### Evidence JSON (additive only)

New keys appear ONLY when a convention is engaged: top-level `replay_conventions`
provenance block; per-allocator `deployed_fraction`/`mean_deployed_fraction`,
`cost_paid`/`total_cost_paid`, `tax_paid`/`total_tax_paid`, `E_executed`,
`integer_residual`, `name_cap_breaches`/`sector_cap_breaches` (+ totals),
`off_universe_liquidations`. No existing key changed. The WF loader now also
stamps `ticker_forward_returns.close_price` onto bars (`AllocatorReplayBar.prices`,
NaN when NULL; the integer-shares engine fails loud on a missing price — no
silent fractional fallback).

## Tests

44 tests in `tests/test_replay_d6_conventions.py` (35 original + 9 new for
the 2026-07-10 correction):
byte-identity pin vs the pre-change fixture; inert all-defaults conventions;
kwarg validation (tax/integer require stateful); exact cash conservation with
cost+tax; deployed-fraction ≠ turnover; carried `w_current` reaches the
allocator (vs stateless zeros contrast pin); off-universe liquidation; tax
short/long boundary (363d → 50%, 365d → 32%) + loss → zero; floor conversion,
executed-never-above-cap post-round, integral shares, post-round carry,
missing-price fail-loud; sector projection down-only + proportional + unmapped
tickers unconstrained + per-name clip + stateless/stateful breach counters;
CLI end-to-end flags, default schema unchanged, flag validation, `--cost-bps`
re-stamp, loader price stamping; **new**: `sector_map_coverage_gap` full/
partial/no-map coverage (mixed mapped/unmapped tickers across bars); CLI
fail-closed on partial and on no-sector-map (`invalid_experiment` +
`reason=sector_map_incomplete`); `--allow-partial-sector-map` escape hatch
(proceeds but stays non-decision-grade); execution_fidelity/
promotion_eligible rejection even with FULL sector coverage; default-mode
(no conventions) schema stays additive-only.

Full suite: **1364 passed, 8 skipped, 1 failed** in the authoring environment — the
single failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`)
was pre-existing on pristine origin/main there (verified via `git stash` before any
edit: 1329 passed, same 1 failed). Zero regressions. Re-run 2026-07-10 (post-#443
merge, no rebase needed — main had not moved): **1372 passed, 7 skipped, 0 failed**
— the previously-noted xgboost failure does not reproduce in this environment
(likely an artifact-availability difference between environments, not a code
change); no other discrepancy. Re-run again after this correction: **1487
passed, 7 skipped, 0 failed** — zero regressions, xgboost failure still does
not reproduce here.

## Not implemented / caveats (explicit)

- **Pure close-price marking across sessions** — see returns-consistent pricing
  above; chosen so the paired stateless/stateful comparison shares one return
  series and cash conservation is exact.
- **Wash-sale masks are not derived from carried state** — bars keep their
  stamped masks; deriving masks from stateful sell history is future work.
- The D6 §4 turnover / drawdown gates are estimand-level checks on the evidence
  output (protocol runner's job), not in-arm projections — only the name/sector
  caps are enforced in-arm, as #445 specified.
- **Round-down only, no deferred one-share rescue** (verified against orchestrator
  #443's final merged §2.3 L3 spec, 2026-07-10): the executed-integer-ledger
  fields (`E_executed`, `integer_residual`) match §2.3's ledger convention, and
  quantization is `floor(w·PV/p)` per §2.3's "round DOWN by default" rule — but
  §2.3 additionally permits a round-UP rescue for one share when it fits within
  the per-name cap AND remaining investable headroom, evaluated after round-down
  orders are funded. This harness does not implement that rescue pass (it also
  does not implement §2.3's post-round cap/sector recheck ON THE EXECUTED
  quantities — `--enforce-caps` projects BEFORE quantization, not after). Both
  omissions make the harness's integer-shares convention a conservative
  (floor-only) approximation of the real L3 layer, sufficient for evaluating
  L1/L2 allocator questions (this PR's actual scope) but NOT a literal
  reproduction of L3's most sophisticated rescue/recheck logic — that logic
  belongs in the live `deployment_governor.py`/`deployment_allocator.py`
  implementation (orchestrator#443-D2/D3, tracked in the separate governor PR
  #179), not this replay harness. **This limitation is now MECHANICALLY
  enforced** (2026-07-10 correction above), not just documented: every
  payload with any convention engaged carries
  `execution_fidelity="L1_L2_ONLY"` / `promotion_eligible=False`, and the
  promotion/verdict gate rejects it as decision-grade regardless of the
  underlying significance numbers.
- **L1/L2/combined attribution (§3(a)/(b)/(c))**: this PR provides the
  CONVENTIONS (tax/integer/stateful/caps) that any of the three estimand
  comparisons would run under; it does not itself orchestrate the (a)/(b)/(c)
  decomposition (same-allocator-different-E* vs allocator-variants-at-matched-E*
  vs combined-vs-incumbent) — that estimand-level orchestration is the D6
  protocol runner's job (not yet built), consistent with the turnover/drawdown
  gate note above. The pre-existing allocator registry (`equal_weight_top_k`,
  `inverse_vol_top_k`, etc., from PR #130) already supports running multiple
  allocator variants side by side, which (b) needs; (a)'s same-allocator/
  different-E* comparison and (c)'s combined-system framing are not yet wired
  since neither the Governor's E* output (#179) nor a protocol-runner harness
  exist yet to supply the second arm.

## NEXT

Orchestrator D6 protocol runs (S0 tuning/evaluation splits) can now pass
`--stateful --tax --integer-shares --enforce-caps --sector-map-json <map>
--cost-bps 5 --fwd-horizon-days 1` for L1/L2-only convention-faithful arms
(NOT decision-grade / NOT promotable — see the mechanical
`execution_fidelity`/`promotion_eligible` stamp above; full L3 fidelity
requires the live governor implementation, `pipeline#179`, already merged).
Independent of (and not touching) that governor PR's kernel files.
