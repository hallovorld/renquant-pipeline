# Fill-truth in the runs DB (#484 §7.3 / §8 item 8, fix D)

**Date:** 2026-07-11
**Owner:** renquant-pipeline (`kernel/persistence.py` + `kernel/trade_events.py`
— the run-record contract the umbrella runner writes through)
**Status:** contract + write-time stamping + emit-ready outcome writer merged-
pending-review; live-path outcome wiring is a one-line umbrella landing (below)

## Why

Orchestrator #484 (ZM/NFLX forensics) §7.3: the `trades` table records
`buy_pending` intent rows with NO outcome — 5 ZM "buy" orders produced 5
pending rows and **0 broker fills**, yet nothing in the DB distinguished a
canceled intent from a fill. Every DB consumer (and ntfy notices) overcounted
buys; establishing basic facts required the broker API.

## What (the contract)

Additive `trades` columns (old DBs migrate via `_COLUMN_MIGRATIONS`; old rows
read back with NULL = unknown, never assumed filled):

| column | meaning |
|---|---|
| `broker_order_id` TEXT | broker-assigned order id (Alpaca uuid); indexed |
| `fill_status` TEXT | `submitted` \| `partially_filled` \| `filled` \| `canceled` \| `rejected` \| `expired` |
| `filled_qty` REAL | broker-confirmed filled quantity |
| `fill_price` REAL | broker-confirmed average fill price |
| `fill_updated_at` TEXT | ISO-8601 UTC of the last outcome update |

Population, two halves:

1. **Write time** (`record_trades`): `broker_order_id` lifted from the event
   (`broker_order_id` → `order_id` → `decision_inputs.order_id`, the key live
   attempt rows already carry); `*_pending` actions stamped
   `fill_status='submitted'`; explicit fill fields pass through. The event
   builders (`build_buy_trade_event` / `build_sell_trade_event`) forward these
   keys, so the umbrella's `runner_trace` attempt rows get order ids with no
   umbrella change.
2. **Post-execution** (`record_order_outcomes(conn, outcomes, run_id=None)`,
   NEW): UPDATE by `broker_order_id` (fallback: explicit
   `ticker`+`trade_date`[+`action`] for pre-contract rows); normalizes broker
   vocabulary (`cancelled`→`canceled`, `new`/`accepted`→`submitted`, unknown
   states kept verbatim lowercased); returns rows updated; fail-soft on
   disabled persistence / unknown ids / malformed entries.

## Landing note (consumer wiring — umbrella-owned, NOT in this PR)

Per the R-PIN/audit doctrine (no new umbrella ownership) the emit-ready seam
ships here; the live path wires it with one line per site in
`RenQuant/backtesting/renquant_104/adapters/runner.py` after sync:

- **Fills** — where broker execution results are reconciled (post
  `broker_order_execution` / morning broker sync):
  `record_order_outcomes(self._db, [{"broker_order_id": o.id, "fill_status": o.status, "filled_qty": o.filled_qty, "filled_avg_price": o.filled_avg_price, "filled_at": o.filled_at} for o in synced_orders])`
- **Pre-open cancels** — the runner already reads
  `logs/alerts/preopen_cancel_ledger.jsonl` (`_preopen_cancel_symbols`,
  runner.py:90-108); the ledger rows carry `order_id`:
  `record_order_outcomes(self._db, [{"broker_order_id": r["order_id"], "fill_status": "canceled"} for r in ledger_rows_today])`
- Optional wording fix from #484 §8 item 8: ntfy buy notices should read
  "submitted (fills at next open pending pre-open re-check)" — owned by the
  notification producer, not this contract.

## Evidence

- `tests/test_fill_truth_persistence.py` (16 tests): the ZM shape (pending →
  canceled, 0 fills — the DB now answers "was ZM bought?" without the broker
  API); the NFLX fill shape (qty+price+timestamp write-back); write-time
  `submitted` stamping with order-id lift; fallback matching; run-id scoping;
  fail-soft paths; legacy-DB migration (today's schema minus the 5 columns →
  migrates, old rows read NULL); `ensure_schema` idempotence; builder
  passthrough.
- Full suite: 1677 passed, 8 skipped.
- Backward compatibility: additive columns only; the new
  `idx_trades_broker_order` index is created AFTER column migrations in
  `ensure_schema` so legacy DBs migrate cleanly; no positional-column
  consumers of `trades` exist in-repo (checked).

## Boundaries

No broker calls, no umbrella edits, no execution-repo edits (the pre-open
cancel gate stays decoupled — its ledger is the wiring surface). Fix C of the
same forensics (model-identity tripwire) is orchestrator-owned: PR #485.
