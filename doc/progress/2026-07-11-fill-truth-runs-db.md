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
   NEW — round 2 post Codex review on #190): outcome mutation is keyed by
   **broker order identity ONLY** (`broker_order_id`/`order_id`; the
   ticker+date fallback was dropped — it cannot distinguish multiple
   same-ticker attempts/cancels in one day). Unmatched outcomes (no order id,
   or no matching row) become explicit `ORDER_OUTCOME_UNMATCHED` rows in the
   append-only `reconciliation_actions` audit table + a warning log — never a
   guessed match. **Monotonic transitions** (rank submitted/unknown=1 <
   partially_filled=2 < canceled/rejected/expired=3 < filled=4): a
   lower-ranked out-of-order/late event is counted `stale` and ignored (late
   `accepted` never overwrites a fill; a cancel never overwrites a full
   fill); `filled_qty` never decreases — a same-or-higher-rank event
   reporting a SMALLER quantity has its qty/price clamped to the prior
   recorded value (never applied) AND is counted `qty_regressed` + logged
   (an observable anomaly, not a silent clamp); a partial fill followed by
   a cancel retains the executed qty+price; a late fill after a recorded
   cancel is broker truth and applies; exact replays are idempotent.
   Unrecognized broker vocabulary maps to the EXPLICIT `unknown` state
   (logged) — never interpretable as canceled/unfilled. Returns observable
   counts `{updated, stale, unmatched, skipped, qty_regressed}`; fail-soft
   throughout (disabled persistence / malformed entries never raise, never
   silently vanish).

### Review round (Codex #190 CHANGES_REQUESTED — all points taken)

1. ticker+date[+action] fallback removed from mutation; broker_order_id
   mandatory; legacy unmatched rows stay untouched (`fill_status` NULL =
   never reconciled) with an `ORDER_OUTCOME_UNMATCHED` audit entry.
2. Monotonic transition rules + out-of-order tests added (late submitted vs
   filled; cancel vs filled; partial→cancel qty/price retention; qty
   never-decrease; late-fill-after-cancel; idempotent replay; two
   interleavings of the same event set converge to the same state).
3. Unknown broker vocabulary → explicit `unknown` state (distinct from NULL
   = pre-contract/never-reconciled); all fail-soft paths counted + logged.
4. **Same-day addendum** (small additive follow-up on top of the round-2
   commit, still same review cycle): a `filled_qty` decrease at the SAME or
   a HIGHER rank (e.g. `partially_filled` qty=2 → `partially_filled` qty=1,
   or `filled` qty=3 → `filled` qty=2 — neither is a rank regression, so
   neither was counted `stale`) was being clamped correctly but silently —
   no counter, no log. Per Codex's literal ask ("log/flag this as an
   anomaly rather than silently applying it"), this is now counted
   `qty_regressed` and logged at `warning`, distinct from `stale`. Two new
   tests pin this (`test_filled_qty_never_decreases`,
   `test_filled_qty_regression_at_filled_rank_is_also_flagged`).

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

- `tests/test_fill_truth_persistence.py` (27 tests): the ZM shape (pending →
  canceled, 0 fills — the DB now answers "was ZM bought?" without the broker
  API); the NFLX fill shape (qty+price+timestamp write-back); write-time
  `submitted` stamping with order-id lift; unmatched-outcome auditing (no
  guessing); the 9 monotonic/out-of-order/replay/convergence cases
  (including the `qty_regressed` observability addendum at both
  `partially_filled` and `filled` rank); unknown-vocabulary state; run-id
  scoping; fail-soft counted paths; legacy-DB migration (today's schema
  minus the 5 columns → migrates, old rows read NULL); `ensure_schema`
  idempotence; builder passthrough.
- Full suite: 1680 passed, 8 skipped, plus 3 pre-existing failures unrelated
  to this change (a D6 replay evidence-pin float mismatch and an XGBoost
  artifact scorer test — both fail identically on the unmodified branch tip,
  i.e. environment/fixture drift, not a regression from this work).
- Backward compatibility: additive columns only; the new
  `idx_trades_broker_order` index is created AFTER column migrations in
  `ensure_schema` so legacy DBs migrate cleanly; no positional-column
  consumers of `trades` exist in-repo (checked).

## Boundaries

No broker calls, no umbrella edits, no execution-repo edits (the pre-open
cancel gate stays decoupled — its ledger is the wiring surface). Fix C of the
same forensics (model-identity tripwire) is orchestrator-owned: PR #485.
