# Fill-truth in the runs DB (#484 §7.3 / §8 item 8, fix D)

**Date:** 2026-07-11
**Owner:** renquant-pipeline (`kernel/persistence.py` + `kernel/trade_events.py`
— the run-record contract the umbrella runner writes through)
**Status:** contract + write-time stamping + emit-ready outcome writer merged-
pending-review (round 3: canonical account+order identity + DB-level atomicity
landed). This is an **INACTIVE, pipeline-owned persistence contract**:
activation is a separate renquant-execution integration (ownership/cutover
contract below); **no umbrella runtime landing** (round 4 correction)

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
| `broker_account_id` TEXT | broker account id; part of the canonical order identity (round 3) |
| `broker_order_id` TEXT | broker-assigned order id (Alpaca uuid); indexed |
| `fill_status` TEXT | `submitted` \| `partially_filled` \| `filled` \| `canceled` \| `rejected` \| `expired` |
| `filled_qty` REAL | broker-confirmed filled quantity |
| `fill_price` REAL | broker-confirmed average fill price |
| `fill_updated_at` TEXT | ISO-8601 UTC of the last outcome update |

Population, two halves:

1. **Write time** (`record_trades`): `broker_order_id` lifted from the event
   (`broker_order_id` → `order_id` → `decision_inputs.order_id`, the key live
   attempt rows already carry); `broker_account_id` lifted the same way
   (`broker_account_id` → `account_id` → `decision_inputs`, round 3); `*_pending`
   actions stamped `fill_status='submitted'`; explicit fill fields pass
   through. The event builders (`build_buy_trade_event` /
   `build_sell_trade_event`) forward the order-id keys today; **follow-up
   needed**: neither builder emits `broker_account_id` yet (no upstream
   broker-account concept exists in this single-account system), so a future
   caller supplying a real account id will get `no_matching_row` against rows
   written before that plumbing lands. That plumbing is prerequisite (a) of
   the renquant-execution integration named in the ownership/cutover contract
   below — execution-owned, intentionally NOT implemented here.
2. **Post-execution** (`record_order_outcomes(conn, outcomes, run_id=None)`):
   outcome mutation is keyed by the **canonical identity
   `(broker_account_id, broker_order_id)`** (round 3 — round 2 had made
   `broker_order_id` mandatory, but it alone is not a unique row identity: the
   same id can legitimately be logged on more than one row, e.g. a
   resubmission or a cross-run logging duplicate). An outcome missing either
   half of the identity, matching no row, or matching MORE THAN ONE row after
   narrowing by this identity, is recorded as an explicit
   `ORDER_OUTCOME_UNMATCHED` audit row (reasons `no_broker_account_id` /
   `no_broker_order_id` / `no_matching_row` / `ambiguous_match`) — never a
   guessed or mass-applied match; `run_id` remains an optional additional
   narrowing filter. **Monotonic transitions** (rank submitted/unknown=1 <
   partially_filled=2 < canceled/rejected/expired=3 < filled=4): a
   lower-ranked out-of-order/late event is counted `stale` and ignored (late
   `accepted` never overwrites a fill; a cancel never overwrites a full
   fill); a **same-ranked** event whose own origin timestamp is OLDER than
   what is already recorded is also `stale` and rejected outright (round 3 —
   checked before the qty-regression clamp, so an old same-rank event cannot
   partially apply); `filled_qty` never decreases — a same-or-higher-rank
   event reporting a SMALLER quantity has its qty/price clamped to the prior
   recorded value (never applied) AND is counted `qty_regressed` + logged
   (an observable anomaly, not a silent clamp); a partial fill followed by
   a cancel retains the executed qty+price; a late fill after a recorded
   cancel is broker truth and applies; exact replays are idempotent. Each
   outcome's match-and-apply is now an **atomic DB-level transaction**
   (round 3 — `BEGIN IMMEDIATE` / `COMMIT`/`ROLLBACK`), closing the
   read-then-write race between two connections against the same on-disk
   file (`get_connection` opens in autocommit `isolation_level=None`, so
   there was no implicit transaction to ride on); lock contention
   (`sqlite3.OperationalError`) propagates to the caller rather than being
   swallowed as an ordinary stale/skip outcome. Unrecognized broker
   vocabulary maps to the EXPLICIT `unknown` state (logged) — never
   interpretable as canceled/unfilled. Returns observable counts
   `{updated, stale, unmatched, skipped, qty_regressed, ambiguous}`;
   fail-soft throughout (disabled persistence / malformed entries never
   raise, never silently vanish).

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

### Round 3 (this pass — Codex's newest CHANGES_REQUESTED, 2026-07-11T18:42:06Z)

Codex flagged two remaining P0/P1 integrity gaps after round 2:

1. **Ambiguous match / mass-update risk.** `broker_order_id` was indexed but
   not unique, and `record_order_outcomes` SELECTed-then-UPDATEd *every* row
   matching it when `run_id` was omitted — the existing test already
   demonstrated duplicate shared-id rows across runs, so a normal
   broker-sync call with no `run_id` would have mutated both. **Fix:** added
   `broker_account_id` (additive column, same `_COLUMN_MIGRATIONS` +
   `ALTER TABLE` pattern as the other fill-truth columns); canonical order
   identity is now `(broker_account_id, broker_order_id)`, both mandatory on
   an outcome dict (missing either → `_record_outcome_unmatched` with
   `no_broker_account_id` / `no_broker_order_id`, same as before — never
   defaulted to a sentinel account). If, even after narrowing by this pair,
   MORE THAN ONE row still matches, NEITHER is updated: the outcome is
   recorded as `ambiguous_match` with the conflicting rowids named in the
   audit detail, and counted under a new `counts["ambiguous"]` key. Chose
   the additive-column + reject-on-ambiguity approach over introducing a
   separate `order_outcomes` table (Codex's offered alternative) — smaller,
   lower-risk change to an already-shipped, already-tested contract; no
   structural blocker was hit that would have forced the bigger table split.
   New test: `test_duplicate_shared_id_without_run_id_is_ambiguous` (seeds
   two rows sharing one `(broker_account_id, broker_order_id)` pair across
   two runs, calls with no `run_id`, asserts `ambiguous==1`, `updated==0`,
   neither row mutated, and an `ORDER_OUTCOME_UNMATCHED`/`ambiguous_match`
   audit row naming both rowids).
2. **In-process-only monotonicity / concurrent race.** The SELECT-then-UPDATE
   loop ran as ordinary statements with no explicit transaction/locking — a
   TOCTOU race between two connections (both could read `submitted` before
   either writes; whichever UPDATE physically landed last would "win"
   regardless of rank). **Fix:** each outcome's match-and-apply now runs
   inside an explicit `BEGIN IMMEDIATE` / `COMMIT`/`ROLLBACK` — `BEGIN
   IMMEDIATE` acquires SQLite's RESERVED lock before the SELECT, so a second
   connection's own `BEGIN IMMEDIATE` blocks until the first transaction
   commits, forcing it to re-read the fresh (already-updated) state rather
   than acting on a stale read. `sqlite3.OperationalError` ("database is
   locked") from a `BEGIN IMMEDIATE` that cannot acquire the lock propagates
   to the caller — legitimate contention is not silently treated as an
   ordinary stale/skip outcome. Also added the timestamp comparison Codex
   asked for: for a **same-rank** event, the incoming `stamped_at` (the
   broker's own event timestamp when supplied) is compared against the
   row's recorded `fill_updated_at`; an older same-rank event is rejected
   outright (`stale`) — checked BEFORE the qty-regression clamp, so it can't
   partially apply. New tests: `test_two_connection_race_converges_to_filled`
   (TWO real `sqlite3.connect()` connections to the same on-disk file,
   driven from real `threading.Thread`s synchronized on a `threading.Barrier`
   so both threads' read phase lines up before either writes, repeated 20x
   with alternating launch order — asserts the final state is always
   `filled` regardless of which thread's write physically lands second);
   `test_same_rank_older_event_does_not_clobber_newer` /
   `test_same_rank_newer_event_still_applies` (the new timestamp-ordering
   branch, both directions). Verified the race test actually discriminates:
   manually reverted the `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` calls to
   no-ops and confirmed the same test then fails (state corrupts to
   `partially_filled`) — then restored the fix and confirmed byte-identical
   to the shipped version before re-running the suite.

Both fixes stay inside `kernel/persistence.py` (pure persistence-layer
engineering, no broker-adapter internals): `record_trades` was extended to
lift `broker_account_id` at write time (mirroring the existing
`broker_order_id` lift) so intent rows carry the new half of the identity
whenever a caller supplies it, but no upstream builder currently does (see
the "follow-up needed" note above) — a documented gap, not silently
papered over.

## Ownership / cutover contract (activation — NOT in this PR)

Round 4 correction (Codex review): earlier drafts of this section proposed
one-line wiring in `RenQuant/backtesting/renquant_104/adapters/runner.py` and
called it an "umbrella landing" — **withdrawn**. The umbrella is not an
accepted production target; **no new umbrella runtime landing is permitted**
(R-PIN/audit doctrine).

The standing contract:

- **This PR is, and remains, a pipeline-owned, INACTIVE persistence
  contract.** It ships the schema, the write-time stamping, and the
  emit-ready `record_order_outcomes()` API; nothing in production calls the
  outcome writer until the execution-owned integration below lands.
- **Activation prerequisite — a separate renquant-execution integration**
  (repo: `renquant-execution`, the broker execution and order-audit plane),
  with two parts:
  - **(a) intent-time identity emission:** the execution-owned order plane
    emits `broker_account_id` — sourced from
    `renquant_execution.broker.BaseBroker.get_account_id()` (implemented by
    `AlpacaBroker.get_account_id()`, Alpaca `account_number`) — alongside
    `broker_order_id` on every order intent it reports, so intent rows carry
    the full canonical identity `(broker_account_id, broker_order_id)` at
    write time. The natural emission point is the order-lifecycle event API
    (`renquant_execution.order_lifecycle.build_order_lifecycle_event` /
    `lifecycle_event_from_confirmation`).
  - **(b) canonical outcome submission after broker reconciliation:** the
    same renquant-execution integration submits outcome dicts (status / qty /
    price / broker event timestamp, keyed by the canonical identity) to
    `record_order_outcomes()` after its broker reconciliation pass — the
    plane that already owns the order lifecycle, the order state machine,
    and the pre-open cancel ledger.
- **Rows without account identity intentionally remain unreconciled.** A row
  written before prerequisite (a) lands (no `broker_account_id`) can never be
  matched by an outcome; any attempted outcome against it is recorded as an
  explicit `ORDER_OUTCOME_UNMATCHED` audit entry and the row keeps
  `fill_status` NULL/`submitted`. This is the fail-closed design, not a
  defect: a row that cannot be bound to a canonical broker identity is never
  guessed into an outcome.
- **Orchestrator may consume the resulting run facts** (read-only runs-DB
  queries for monitors/forensics, e.g. the #485 model-identity/outage
  monitor plane) — it does not write them.
- **No broker adapter code ships in this pipeline PR** (and none did): the
  boundary between the persistence contract (here) and the broker/order
  plane (renquant-execution) stays intact.
- Non-blocking, notification-producer-owned: the #484 §8 item 8 ntfy wording
  fix ("submitted (fills at next open pending pre-open re-check)") belongs
  to whoever owns the notices, not to this contract.

## Evidence

- `tests/test_fill_truth_persistence.py` (34 tests, +7 this round): the ZM
  shape (pending → canceled, 0 fills — the DB now answers "was ZM bought?"
  without the broker API); the NFLX fill shape (qty+price+timestamp
  write-back); write-time `submitted` stamping with order-id + account-id
  lift; unmatched/ambiguous-outcome auditing (no guessing, no mass-update);
  the monotonic/out-of-order/replay/convergence cases (rank-based +, new
  this round, same-rank timestamp-based); a real two-connection concurrent
  race proving `filled` wins regardless of thread write order; run-id
  scoping vs. the no-run_id ambiguous case; fail-soft counted paths;
  legacy-DB migration (today's schema minus all 6 fill-truth columns →
  migrates, old rows read NULL); `ensure_schema` idempotence; builder
  passthrough.
- Full suite (via the shared checkout's `.venv` interpreter, since a fresh
  git worktree has no `cvxpy`/etc. of its own): 1687 passed, 8 skipped, plus
  3 pre-existing failures unrelated to this change (a D6 replay
  evidence-pin float mismatch and an XGBoost artifact scorer test) —
  reconfirmed independently on an unmodified worktree of the same branch
  tip (4b8eece7): identical 3 failures, so this is environment/fixture
  drift, not a regression from this work.
- Concurrency-test discriminating power verified directly: temporarily
  neutered the `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` calls to no-ops (byte
  diff confirmed, then restored) and reran
  `test_two_connection_race_converges_to_filled` — it failed (state
  corrupted to `partially_filled`), confirming the test would have caught
  the pre-fix code.
- Backward compatibility: additive columns only; the new
  `idx_trades_broker_order` / `idx_trades_broker_account_order` indexes are
  created AFTER column migrations in `ensure_schema` so legacy DBs migrate
  cleanly; no positional-column consumers of `trades` exist in-repo
  (checked).

## Boundaries

No broker calls, no umbrella edits, no execution-repo edits, no broker
adapter code — this PR is the persistence contract only, INACTIVE until the
renquant-execution integration named above lands. Fix C of the same
forensics (model-identity tripwire) is orchestrator-owned: PR #485.
