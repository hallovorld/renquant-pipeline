# Replay harness: opt-in D6 protocol conventions (stateful / tax / integer shares / in-arm caps)   (PR #180)

STATUS:    delivered (r1 + r2 review requirements folded in)
WHAT:      Adds the D6 §1.1/§2.3/§4 frozen replay conventions (tax drag,
           full L3 integer-aware execution with deferred one-share rescue
           and post-round rechecks, stateful sessions, fail-closed in-arm
           sector caps, execution-fidelity evidence stamps) to the
           allocator replay harness as strictly opt-in kwargs/CLI flags;
           defaults stay byte-for-byte identical to pre-change behavior.
WHY/DIR:   D6 preregistered replay protocol (orchestrator#443, merged) —
           the exploratory run documented in orchestrator#445 found the
           harness couldn't honor D6 §1.1's frozen conventions: (1) no tax
           drag, (2) no whole-share quantization, (3) stateless sessions
           (deployed fraction ≡ turnover; hysteresis unevaluable), (4)
           sector caps not enforced inside arms (35% gate breached by every
           arm silently at ~5.7 candidates/session breadth). Codex r1 on
           #180 ruled floor-only quantization insufficient for the primary
           cash-drag/deployed-fraction estimand → FULL L3 implemented here.
EVIDENCE:  n/a (harness/tooling change; Tests below for regression-pinning
           evidence, incl. a hand-computed deployment-understatement case)
NEXT:      A D6 protocol runner (not yet built) to orchestrate the
           §3(a)/(b)/(c) estimand decomposition over this harness.

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
| Whole-share quantization in all arms | `--integer-shares`: full RFC #443 §2.3 L3 executed-state invariant (see next section); per-session `E_executed` + `integer_residual` + `rescue_buys` + `recheck_capdowns` |
| Fill at session close price | share conversion anchors to the session `close_price` at (re-)entry; held positions are marked by the same per-bar `fwd_return` the stateless harness uses ("returns-consistent pricing" — see caveat) |
| §4 caps: name ≤ 12%, sector ≤ 35% | `--enforce-caps` + `--sector-map-json`: down-only projection INSIDE the arm before returns; per-session breach counters replace silent allowance; FAIL-CLOSED on incomplete sector coverage (r2) |
| Deployed fraction as an estimand; hysteresis | `--stateful`: positions/lots/cash carried across sessions per arm; allocators receive the carried `w_current`; `deployed_fraction` series in evidence |

### RFC #443 §2.3 L3 — integer-aware execution (r1 scope ruling: FULL option)

Codex r1 CHANGES_REQUESTED correctly identified that floor-only quantization can
systematically understate deployment at this portfolio size, invalidating the
primary cash-drag/deployed-fraction estimand — a floor-only run is NOT
convention-faithful and must not be used as deployed-fraction or promotion
evidence (the `execution_fidelity` stamp below enforces this mechanically).
`_execute_integer_session` (`allocator_replay.py`) implements the final merged
L3 contract, mirroring the production implementation
(`kernel/pipeline/governor_sizing.py` `_fill_buys` + the S6 A-3 deferred
one-share rescue in `kernel/pipeline/task_selection.py`):

1. **Round DOWN default** — buy legs `floor(Δw·PV/p)` in conviction order
   (shrunk-Kelly raw desc per the RFC's "conviction, defined", ticker tiebreak),
   headroom-aware; sell legs `floor(Δw·PV/p)` with full liquidation at target≈0.
2. **Deferred one-share rescue** — AFTER all round-down orders fund, leftover
   investable headroom (cash − snapshot cash reserve, the task_selection
   convention) is re-offered one share at a time in conviction order to names
   still short of target (a floored-to-0 candidate rounds UP to exactly one
   share) — each share only iff it fits the per-name cap AND the remaining
   headroom; a name can overshoot its target by at most one share.
3. **Post-round rechecks on EXECUTED quantities** — cash (incl. reserve;
   headroom-bounded fills make it hold by construction), single-name cap,
   sector caps (snapshot families + D6 map when `--enforce-caps`), and
   correlation-pair constraints; a violating BUY is capped down one share at a
   time, lowest conviction first — never carried in breach. Carried-drift
   breaches are not orders and stay visible via the violation accounting.
4. **Ledger** — per-session `E_executed`, `integer_residual = Σtarget −
   E_executed`, plus `rescue_buys` / `recheck_capdowns` counters.

Documented divergences from main's L3 (harness necessarily generalizes):
- Sells are the arm's unconditional decisions (no improvement-positive pair
  veto — that is governor pair-rotation logic, not a replay convention); buys
  are therefore funded by post-sell cash in one pass rather than main's
  interleaved `_fill_buys`/pair-sell/`_fill_buys` loop.
- Both fill passes are bounded by the reserve-adjusted headroom (main's
  governor main pass bounds by raw cash; the RFC recheck demands executed
  cash incl. reserve, so the harness enforces it at fill time — equivalent
  outcome, never in breach).
- Main relies on L2 for sector/corr feasibility pre-round and carries the
  ≤1-share sell overshoot; the harness rechecks executed BUY quantities
  against sector/corr/name caps explicitly because replay arms are arbitrary
  allocators with no L2 guarantee.

### r2: sector-map fail-closed + execution-fidelity evidence contract

- **Sector-map fail-closed** — with `--enforce-caps`, a sector map that does
  not cover EVERY active ticker in EVERY replay bar is a hard error (fail
  closed), both at the CLI (missing `--sector-map-json`) and per-bar inside
  the arm (mixed mapped/unmapped universes). The permissive behavior survives
  only behind the explicit exploratory flag `--allow-unmapped-sectors`
  (`ReplayConventions.allow_unmapped_sectors`), and any run using it is
  stamped non-decision-grade.
- **Execution-fidelity stamps** — every engaged-conventions evidence payload
  carries `replay_conventions.execution_fidelity` (`"L3_FULL"` only when
  stateful + tax + integer-shares + enforce-caps with fail-closed sector
  coverage are ALL engaged; anything less is `"L1_L2_ONLY"`) and
  `promotion_eligible`. The promotion/evidence gate REJECTS any
  engaged-conventions payload without `L3_FULL` fidelity: significance blocks
  are forced `diagnostic_only` / non-promotable with an explicit
  `promotion_block_reason`, and the verdict cannot name a promotion candidate.
  Default-mode (no conventions) payloads are untouched — that evidence class
  predates D6 and keeps its byte-identical schema and existing
  `constraint_fidelity` gating.

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
provenance block (now incl. `execution_fidelity`, `promotion_eligible`, sector
coverage mode); per-allocator `deployed_fraction`/`mean_deployed_fraction`,
`cost_paid`/`total_cost_paid`, `tax_paid`/`total_tax_paid`, `E_executed`,
`integer_residual`, `rescue_buys`/`recheck_capdowns` (+ totals),
`name_cap_breaches`/`sector_cap_breaches` (+ totals),
`off_universe_liquidations`. No existing key changed. The WF loader now also
stamps `ticker_forward_returns.close_price` onto bars (`AllocatorReplayBar.prices`,
NaN when NULL; the integer-shares engine fails loud on a missing price — no
silent fractional fallback).

## Tests

Tests in `tests/test_replay_d6_conventions.py`:
two-tier default-mode pin (deep-exact everywhere + byte-identity on the
minting platform); inert all-defaults conventions; kwarg validation
(tax/integer require stateful); exact cash conservation with cost+tax;
deployed-fraction ≠ turnover; carried `w_current` reaches the allocator (vs
stateless zeros contrast pin); off-universe liquidation; tax short/long
boundary (363d → 50%, 365d → 32%) + loss → zero; floor conversion,
executed-never-above-cap post-round, integral shares, post-round carry,
missing-price fail-loud; L3: floored-to-0 rescue to exactly one share,
rescue blocked by per-name cap / by reserve headroom, leftover-cash
conviction ordering, deployment-not-understated at small PV (hand-computed
0 → 0.903), name/sector/corr post-round cap-downs (lowest conviction first,
zero recorded violations post-recheck), cash reserve never breached; r2:
sector-map fail-closed on mixed mapped/unmapped universes (+ CLI), the
exploratory flag path stamped non-decision-grade, execution-fidelity stamps,
and the negative gate test (a non-L3_FULL payload cannot name a promotion
candidate even when DSR/PBO pass); sector projection down-only +
proportional + per-name clip + stateless/stateful breach counters; CLI
end-to-end flags, default schema unchanged, flag validation, `--cost-bps`
re-stamp, loader price stamping.

Full suite (local, on main @ 9117f89 post-#179-merge): see PR body for the
final counts — the single environmental failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`)
is pre-existing on pristine origin/main in this environment (fails
identically without this branch's changes; passes in CI). Zero regressions.

## Not implemented / caveats (explicit)

- **Pure close-price marking across sessions** — see returns-consistent pricing
  above; chosen so the paired stateless/stateful comparison shares one return
  series and cash conservation is exact.
- **Wash-sale masks are not derived from carried state** — bars keep their
  stamped masks; deriving masks from stateful sell history is future work.
- The D6 §4 turnover / drawdown gates are estimand-level checks on the evidence
  output (protocol runner's job), not in-arm projections — only the name/sector
  caps are enforced in-arm, as #445 specified.

## NEXT

Orchestrator D6 protocol runs (S0 tuning/evaluation splits) can now pass
`--stateful --tax --integer-shares --enforce-caps --sector-map-json <map>
--cost-bps 5 --fwd-horizon-days 1` for convention-faithful (L3_FULL) arms.
Rebased/merged onto main @ 9117f89 (post-#179 merge); the governor/allocator
kernel files are consumed read-only (`shrunk_kelly_raw` as the RFC conviction
ordering key), never modified.
