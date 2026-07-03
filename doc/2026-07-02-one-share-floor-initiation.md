# S6 A-3: one-share floor for high-price initiations (flag, default OFF)

Program reference: capability program §1.2 A-3; RS-2 lane-A timing memo
(2026-07-02).

## Problem (2026-07-01 OXY forensics)

The multiplicative sizing stack (Kelly × conviction × σ-mult × PV) can
compound a target notional below ONE share of a high-price name — e.g. BLK
target $324 < 1 share ≈ $1,100. The whole-share sizer returns 0 shares, the
name is dropped as `size_insufficient_cash`, and selection structurally
drifts toward LOW-price names (OXY partially won *because* it is cheap, not
because it was the better candidate).

## Fix (round 1)

Under `sizing.one_share_floor_enabled` (default **false** — inert until
strategy-104 defines it), a candidate that zeroes out ONLY because of
whole-share rounding rounds UP to exactly one share iff:

  (a) one share ≤ regime `max_position_pct` × PV,
  (b) one share ≤ investable headroom after cash reservations,
  (c) the name already passed EVERY admission gate above (sizing-only change).

Every round-up is stamped with a dedicated ledger field
(`size_floor_reason = "one_share_floor_round_up"`), so it's auditable.
Flag absent ⇒ byte-identical to pre-PR behaviour (pinned by regression tests).

## Round 2 (codex review): intended-notional contract, edge cases

Codex's concern: proof this repairs whole-share rounding only, not a
broader change to the sizing contract. Made the intended-notional contract
explicit — every input regime classified as NORMAL / RESCUED / ZERO /
FLAG-OFF (see the docstring block in `tests/test_one_share_floor_initiation.py`).

Investigating the ZERO regime (`max_pct <= 0` — a genuine "invest nothing"
decision, not a rounding artifact) surfaced a **real, live bug**: the legacy
(non-Kelly) sizing path had no zero-target guard, unlike the Kelly path
(which already `continue`s on `max_pct <= 0` before reaching the floor).
`conviction_multiplier`/`sigma_multiplier` can legitimately return exactly
`0.0` (e.g. `min_mult: 0.0` config, at-or-below-floor candidate) — and
pre-fix, that zero-conviction candidate was WRONGLY floor-rescued to 1 share
whenever price happened to fit the regime cap and investable cash.
Reproduced directly: BLK @ $1,100, conviction=0.0 exactly, floor rescued it
anyway. Fixed with a `max_pct > 0` eligibility guard, scoped narrowly so
flag-OFF behaviour and the block-reason string are unchanged.

Also added: a frozen OFF-vs-ON sweep over a small multi-candidate panel
proving the rescue changes exactly the rounds-to-zero candidate and nothing
else — the first evidence at more than one ticker, though still not a full
portfolio-construction pass.

## Round 3 (codex review): portfolio-level preregistered shadow evidence

Codex's finding: round 1/2 proved the sizing *function* correct in
isolation, but that's unit correctness, not operational authorization —
even a function that's individually right could still interact badly with
the *rest* of a multi-candidate portfolio pass. Required six specific,
machine-checkable metrics with explicit pass/fail thresholds: changed
ticker/order set, target-vs-realized gross exposure jump, max single-name
concentration, reserve use, turnover/cost delta, score-vs-price-rank drift.

### A real defect, found by building the evidence

Building the portfolio-level comparison surfaced a genuine problem, not
just a documentation gap. The round 1/2 rescue fired **inline**, in the
same greedy pass as normal sizing, decrementing the same `remaining_cash`
every candidate draws from. A rescue ranked ahead of a normal candidate can
consume *more* cash than its own (tiny) target implied — rounding up to a
full share can cost far more than the fractional target that triggered the
rescue in the first place — and that excess can crowd a later, genuinely
higher-conviction candidate out of the session entirely.

Measured on a constructed 5-name panel (`MARGINAL` panel_score=0.5 price=$700,
`BKNG` 0.7/$5,000, `OXY` 0.6/$48, `BLK` 0.001/$1,100 — the round-1 rescue
candidate, `NEG` -0.11/$30 gate-blocked) with cash tight enough to make the
rescue's cost bite:

| | OFF | ON (pre-fix, inline rescue) |
|---|---|---|
| MARGINAL | 1 share, $700 | **dropped** (`size_insufficient_cash`) |
| BLK | dropped | 1 share, $1,100 |
| score-vs-realized-invest Spearman ρ | +0.11 | **-0.63** |

A $0.001-conviction rescued name displaced a $0.5-conviction name for cash,
and inverted the score-vs-investment ordering codex named as the exact
failure mode to guard against.

### Fix: deferred second pass, not just more tests

`SizeAndEmitTask` now runs the rescue as a genuinely separate pass. Every
normal candidate sizes fully first, in unchanged rank order, against the
full `remaining_cash` — completely uncontested by any rescue. Only after
every normal candidate has had its shot does the rescue pass spend whatever
cash is genuinely left over, in the same relative order the rescue
candidates were deferred in (so among rescue candidates, higher rank still
wins ties for leftover cash). A rescue can now only **add** a trade using
idle cash — it structurally cannot take cash a normal candidate needed, so
it cannot crowd anyone out or invert an existing candidate's funding.

Re-measured on the same panel with cash widened enough for BLK's rescue to
actually succeed (so the fix isn't just "nothing happens"):

| | OFF | ON (post-fix, deferred rescue) |
|---|---|---|
| MARGINAL | 1 share, $700 | 1 share, $700 (**identical**) |
| OXY | 21 shares, $1,008 | 21 shares, $1,008 (**identical**) |
| BLK | dropped | 1 share, $1,100 (**only** the rescue's own trade) |

### The six metrics, thresholds, and real results

Computed by `tests/test_one_share_floor_initiation.py::_assert_portfolio_evidence`,
run at both 0% and 5% cash-reserve settings — both PASS at both settings:

1. **Changed ticker/order set** — threshold: only rescue-eligible tickers
   may differ; every other ticker byte-identical. Result: PASS — MARGINAL,
   OXY, BKNG, NEG identical in both runs; only BLK (the rescue) differs.
2. **Gross exposure jump** — threshold: ON−OFF total invest must equal
   exactly the sum of rescued positions' own invest dollars. Result: PASS —
   jump = $1,100.00, exactly BLK's rescued invest (reserve=0%); $0.00 at
   reserve=5% (cash too tight for the rescue to fire at all — correctly
   falls back to byte-identical, not a partial/inconsistent state).
3. **Max single-name concentration** — threshold: no position exceeds the
   regime's `max_position_pct` cap, in either run. Result: PASS — max
   observed 10.18% (BLK's rescue) against a 12% cap.
4. **Reserve use** — threshold: total invest ≤ `starting_cash −
   cash_reserve_pct × PV`, in either run. Result: PASS at both reserve
   settings.
5. **Turnover/cost delta** — threshold: gross churn (Σ|invest delta| across
   every ticker) equals exactly the rescued positions' invest dollars —
   zero churn attributable to anything else. Result: PASS — churn = $1,100,
   entirely BLK's own trade.
6. **Score-vs-price-rank drift** — threshold: every OFF-funded (non-rescued)
   ticker's fill is identical ON (not a whole-panel correlation, which
   shifts by construction whenever *any* new position is added — the actual
   risk codex named is an *existing* candidate losing ground, which this
   isolates). Result: PASS — MARGINAL and OXY's shares/invest are exactly
   identical between OFF and ON.

Flag stays default OFF throughout this fix. No production behaviour
changes as a result of this round.

## Verification

- `tests/test_one_share_floor_initiation.py`: 20/20 passed (18 pre-existing
  + 2 new portfolio-level tests, one per reserve setting).
- Full repo suite: 1142 passed, 7 skipped (pre-existing, unrelated).
