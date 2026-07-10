# Replay harness: opt-in D6 protocol conventions (stateful / tax / integer shares / in-arm caps)

**Date:** 2026-07-09
**PR:** feat/replay-harness-d6-conventions
**Context:** D6 preregistered replay protocol (orchestrator #443) — the exploratory
run documented in orchestrator #445 found the allocator replay harness cannot honor
the D6 §1.1 frozen conventions: (1) no tax drag, (2) no whole-share quantization,
(3) stateless sessions (deployed fraction ≡ turnover; hysteresis unevaluable),
(4) sector caps not enforced inside arms (the 35% gate was breached by every arm
at ~5.7 candidates/session breadth, silently).

## What

All conventions are **strictly opt-in** (kwargs on `replay_one_allocator` /
`replay_all` / `run_replay` via a frozen `ReplayConventions` dataclass; CLI flags
on `run_ab_replay.py`). Defaults reproduce the pre-change behavior **byte-for-byte**
— pinned by a fixture evidence JSON generated on origin/main @ f6e818c before any
edit (`tests/fixtures/ab_replay_default_evidence.json` +
`tests/fixtures/d6_default_bars.py`), so all existing committed evidence stays
reproducible.

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

35 new tests in `tests/test_replay_d6_conventions.py`:
byte-identity pin vs the pre-change fixture; inert all-defaults conventions;
kwarg validation (tax/integer require stateful); exact cash conservation with
cost+tax; deployed-fraction ≠ turnover; carried `w_current` reaches the
allocator (vs stateless zeros contrast pin); off-universe liquidation; tax
short/long boundary (363d → 50%, 365d → 32%) + loss → zero; floor conversion,
executed-never-above-cap post-round, integral shares, post-round carry,
missing-price fail-loud; sector projection down-only + proportional + unmapped
tickers unconstrained + per-name clip + stateless/stateful breach counters;
CLI end-to-end flags, default schema unchanged, flag validation, `--cost-bps`
re-stamp, loader price stamping.

Full suite: **1364 passed, 8 skipped, 1 failed** — the single failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`)
is pre-existing on pristine origin/main in this environment (verified via
`git stash` before any edit: 1329 passed, same 1 failed). Zero regressions.

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
--cost-bps 5 --fwd-horizon-days 1` for convention-faithful arms. Independent of
(and not touching) the open governor PR #179 kernel files.
