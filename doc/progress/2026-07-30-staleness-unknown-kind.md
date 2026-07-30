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

## 3. The fix inverts the default

An unrecognised kind is now a **provenance gap**, never a pass. The artifact JSON is
still read best-effort, so a kind that happens to stamp `trained_date` there is
**measured** rather than dismissed — it simply can no longer come back as a silent
pass. Refusing everything unknown would trade a fail-open for a permanent alarm,
which is how a check gets switched off wholesale.

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
