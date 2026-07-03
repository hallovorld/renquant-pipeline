# Parking sleeve (S7) — idempotency guard, explicit SGOV valuation semantics, operational-vs-economic scorecard split

**Date:** 2026-07-03 · **Author:** Claude · **Status:** PR #157 open, round 2 fix

## STATUS

Fixing Codex's round-2 review on `feat/parking-sleeve-shadow` (S7, β-budgeted
SPY/SGOV parking sleeve, shadow mode, flag default-OFF). Flag stays
default-OFF throughout this round — this is purely an instrumentation and
reporting-rigor fix, nothing here enables anything live.

## WHAT

Codex's finding: "I still need the PR to separate operational observation
from economic authorization... idempotent / concurrency-safe shadow state,
explicit SGOV valuation semantics, and separate scorecards for operational
correctness vs economic merit. Without that split, a clean shadow log can
be misread as authorization evidence."

Three fixes in `task_parking_sleeve.py`:

1. **Idempotency + concurrency guard.** `ParkingSleeveShadowTask._run` now
   resolves the log path, acquires an exclusive `fcntl.flock` on a sibling
   `.lock` file, and — *inside* that lock — checks `_has_logged_date(path,
   date_str)` before doing anything else. If a summary row already exists
   for today's date (a retry after a transient failure, or two runs racing
   on the same host), the task increments
   `ctx.counters["parking_sleeve_duplicate_date_skipped"]` and returns
   without appending anything or rolling the shadow book forward a second
   time. The read-last-state → compute-plan → append sequence
   (`_compute_and_log`) only runs once the duplicate-check has passed,
   still holding the lock, so a genuinely concurrent second caller cannot
   race between the check and the append.

2. **Explicit SGOV valuation semantics.** New `SGOV_VALUATION_MODE =
   "cost_no_carry"` constant, stamped into every `book_state` row. SGOV is
   tracked at cost only — its persisted value moves solely via BUY/SELL
   notional, never marked to market from price changes (unlike the SPY leg,
   which *is* marked to market via `shadow_spy_qty * spy_price`). This
   asymmetry was already true in the code (and named in the module
   docstring) but was implicit; it's now an explicit, tested, per-row field
   so a downstream consumer of the raw JSONL cannot misread
   `sleeve_contribution_pct` as capturing SGOV's real T-bill carry.

3. **Two separate scorecards, not one blended one.** New
   `OPERATIONAL_BOOK_STATE_FIELDS` / `ECONOMIC_BOOK_STATE_FIELDS` frozensets
   (disjoint by construction, tested) categorize every `book_state` key.
   New `build_operational_scorecard(rows)` reports pure instrumentation
   hygiene (schema completeness, duplicate-summary-date count — always 0
   while the guard holds, blocked-reason counts, SGOV valuation-mode
   consistency) — it answers "does the logger work," nothing about
   whether the strategy is a good idea. New `build_economic_scorecard(rows)`
   reports the shadow-simulated economic picture (final/mean sleeve
   contribution, max drawdown-budget consumption) with an explicit
   `"authorization_grade": False` field, always — this scorecard is at most
   the beginning of an eventual economic case, never sufficient alone to
   authorize live capital deployment.

## WHY-DIR

A clean operational log (no crashes, no duplicate rows) answers a
completely different question than "is parking cash in SPY/SGOV a good
idea." Blending them into one scorecard risks exactly the failure mode
Codex named: a working logger gets misread as economic authorization
evidence. Keeping the two structurally separate — disjoint field sets,
separate aggregator functions, an explicit `authorization_grade: False` on
the economic side — makes that misreading a type error instead of a human
judgment call.

## EVIDENCE

- 8 new tests: idempotent-rerun-same-date (no duplicate rows, no double
  state application, counter increments exactly once),
  scorecard-reports-zero-duplicate-dates-when-guard-holds,
  SGOV-price-appreciation-alone-does-not-move-sleeve-value,
  SGOV-valuation-mode-stamped-on-every-row,
  scorecards-do-not-share-fields, operational-scorecard-never-reports-
  economic-merit, economic-scorecard-is-explicitly-not-authorization-grade,
  economic-scorecard-empty-log.
- Full repo suite: 1070/1070 passed, 7 pre-existing skips (was 1062/1062
  before this round).
- Flag remains default-OFF and shadow-only throughout; no live-enablement
  behavior touched.

## NEXT

None from this round — awaiting re-review. Live enablement remains
explicitly out of scope (RS-1 §4 authorization bar, unchanged).
