# The staleness rail was fixed once by enumeration; the same shape came back as `blend`

**Date:** 2026-07-30 · GOAL-1 / GOAL-5 · `renquant-pipeline`

**Bottom line:** `P-MODEL-STALENESS` dispatches on `panel_scoring.kind`. Recognised
kinds are checked; **everything else returns a PASS** with
`kind=... unrecognized — staleness skip`. Measured on the live machine today, the
shadow-**blend** lane took that branch **while it was issuing buy recommendations**.

## 1. The three lanes, same day, three different behaviours

`[VERIFIED — logs/daily_104/2026-07-30*.log]`

| lane | `kind` | staleness verdict |
|---|---|---|
| prod | — | `effective_train_cutoff_date` unstamped → **SURFACED** as a gap |
| shadow | `xgb` | `train-cutoff age 624d` → **SURFACED** |
| **blend** | `blend` | **`unrecognized — staleness skip`** — not checked at all |

The blend lane's ntfy that afternoon read
`SHADOW-ACTION | BUY BWXT x4 @ $165.63 | BUY VLO x2 @ $311.73`.

## 2. This defect was already fixed once, the wrong way

The module's own note dated **2026-06-27** says the check previously skipped for
every non-`hf_patchtst` kind, so *"the staleness rail did nothing for the model
actually driving trades"*. The fix **added `xgb` to the allow-list**.

Enumerating leaves the default **fail-open**. Any kind nobody thought of is a silent
pass. Beyond `blend`, committed strategy configs also carry `patchtst` (no `hf_`
prefix) and one with **no kind at all** — both take the same branch
`[VERIFIED — sweep of every `strategy_config*.json`]`.

## 3. The fix inverts the default — and my first version of it was still fail-open

An unrecognised kind is now a **provenance gap, never a pass**.

**Corrected at review (#233).** My first implementation read the dates best-effort
and **passed when they were fresh**. That is still the central fail-open: freshness
being measurable **does not establish** that an unrecognised artifact carries the
schema or training provenance this rail requires, so passing on fresh dates
**silently certifies a new model kind** — the exact extension work that has to stay
visible. It also contradicted this document's own title.

Now: **never a pass, however fresh it looks.** The measured dates are still read and
**reported in the message**, because discarding them would make the finding
unactionable — the reader needs to tell a routine registration from an urgent one.

My "refusing everything unknown trades a fail-open for a permanent alarm" worry was
overweighted: this check is **SOFT**, so a non-pass is a visible finding, not a
blocker. A soft finding on an unregistered kind *is* the signal.

## 4. Suite

`tests/test_staleness_unknown_kind.py` — 11 tests. Five unknown kinds (`blend`,
`patchtst`, `ensemble`, `""`, `None`) must fail without dates; an unknown kind
**with** valid dates must still **pass** (or the fix is a blanket refusal); an
unknown kind with an **old** date must still **alarm** (anti-vacuity — measuring has
to be able to fail, or the best-effort read is the old skip in new wording); an
unreadable artifact fails; the two recognised kinds are unchanged; and
`panel_scoring.enabled = false` still skips, which is the one legitimate skip.

## 5. Two of my own errors, in the tests not the fix

- I asserted on `res.passed` / `res.detail`. The dataclass fields are **`ok`** and
  **`message`**. All 11 tests failed on my harness before the fix was exercised at all.
- My "should pass" fixtures carried only `trained_date`. The module's existing
  contract treats an absent `effective_train_cutoff_date` as **its own** provenance
  gap, so that is not a pass for *any* kind. The failure was my expectation, not the
  code.
