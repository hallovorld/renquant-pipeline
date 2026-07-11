# 2026-07-10 — parking sleeve `mode=live` (SGOV floor), capability-dark

GOAL-6 follow-through on #157 (`ParkingSleeveShadowTask`), which shipped
shadow JSONL logging only and made `sleeve.mode="live"` fall back to shadow
with a warning. This change implements the live path. Lineage: RS-1
(renquant-orchestrator `doc/research/2026-07-02-rs1-parking-sleeve.md`, r2),
104 capability program §1.3 / P1-2, strategy-104 #44 (config +
cumulative-cap semantics).

## What live mode does

`sleeve.enabled=true` + `sleeve.mode="live"` emits REAL order intents,
restricted to the RS-1 §2/§4 **SGOV floor variant**:

* **Buys**: idle cash above `reserve_pv_pct`·PV + pending admitted buys +
  the regime `cash_reserve_pct` reserve is swept into whole-share SGOV.
* **Sells first (free-before-need)**: whenever cash falls short of those
  reserves (e.g. an admitted single-name buy larger than free cash), the
  sleeve emits SGOV sells sized (with the sell-friction multiplier) to cover
  the full shortfall. Sells land in `ctx.exits`, which `pp_execution`
  executes in `ExitsJob` BEFORE `BuysJob` — the liquidity invariant is that
  the sleeve can never cause a main-strategy buy to fail for cash, enforced
  twice (planner reserve arithmetic + an `invest_cap` re-clamp at emission).
* **Cumulative cap**: `max_sleeve_pct` (default 0.50, strategy-104 #44
  semantics) caps total sleeve exposure against the REAL broker SGOV
  holding across sessions, and rebalances an over-cap sleeve back down.
* **SPY arm stays dark**: the planner runs `sgov_only=True` with
  `spy_qty=0`, plus a defensive drop-and-count guard on any non-SGOV
  action. There is deliberately NO config knob for live SPY — RS-1 §4
  requires the SPY arm's own pre-registered comparison + recorded capital
  authorization (mirrors strategy-104's `spy_arm_gate_cleared` guard).
* **Wash-sale**: SGOV buys pass through the existing cost-aware §1091
  engine (`is_wash_sale_blocked_with_cost`) — a recent SGOV LOSS sale
  blocks the re-buy (T-bill ETFs can and do print small losses); gain
  sales pass. Sells are never wash-sale blocked. Sell fills stamp the
  wash-sale clock via the existing `StampWashSaleTask` path unchanged.
* **Buy gates respected, exits always allowed**: `buy_blocked` /
  `skip_buys` / `bear_only` block sleeve buys; sells (incl. the BEAR full
  sweep-off) ignore them.
* **Fail-closed on missing SGOV price**: no buy is ever emitted without a
  positive SGOV price. If cash is needed (reserve/pending shortfall or
  BEAR) while the price is missing, the FULL position is liquidated (a
  full exit needs no price) so free-before-need still holds. Loud
  `log.error` + `parking_sleeve_live_missing_sgov_price` counter.
* JSONL keeps being written (same schema, `book_state.mode="live"`,
  `sgov_valuation_mode="mark_to_market"`); the summary `shadow_state`
  mirrors the REAL post-trade book so a mode flip never inherits a stale
  shadow book. Same-date idempotency guard applies (a same-date retry
  re-plans next session from real broker state instead of re-emitting).

## Shadow regression

Shadow behavior is byte-identical to #157 — including IGNORING a
`max_sleeve_pct` config key (pinned by
`TestShadowByteIdenticalRegression`): the shadow corpus feeds the RS-1 §4
pre-registered comparison and must not silently change mid-collection.
Stated divergence: shadow models the uncapped β-split; live enforces the
#44 cap and SGOV-only floor.

## SGOV data-availability finding (`[VERIFIED]` 2026-07-10)

SGOV daily bars exist in NEITHER umbrella OHLCV store:
`RenQuant/data/ohlcv/` (has SPY, BIL — no SGOV) nor
`RenQuant/backtesting/renquant_104/data/ohlcv/` (158 tickers, SPY only,
no SGOV). `ctx.prices["SGOV"]` will therefore be missing on the live path
today and mode=live will refuse (fail-closed) by design. **Umbrella/
base-data follow-up required before any live flip: add SGOV to the daily
price fetch + serving price map.**

## Rollout state

This PR ships the capability DARK: strategy-104 config still has
`sleeve.enabled=false`, `mode="shadow"`. Flipping to live is a separate,
gated strategy-104 config PR and additionally requires (per RS-1 §4):
the SGOV arm's own recorded capital-authorization decision, and the
umbrella SGOV price coverage above. A clean shadow/operational scorecard
is NOT authorization evidence (see #157's operational-vs-economic split).

## Tests

`tests/test_parking_sleeve.py`: 35 → 56 (+21 net; 22 new — 4 planner
cap/sgov-only, 15 live-mode incl. free-before-need ordering, cumulative
cap, missing-price fail-closed, wash-sale block/pass, BEAR sweep-off,
idempotency, fail-isolation; 2 shadow byte-identical regression; 1
replaced: live-unimplemented → unknown-mode fallback). Full suite:
1568 passed, 7 skipped.
