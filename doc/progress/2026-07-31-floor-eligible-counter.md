# The one-share-floor enablement contract asked for evidence its own instrumentation could not produce

**Date:** 2026-07-31 · `renquant-pipeline` · deployment blockers (orch task #14)

STATUS:    measurement only, 7 tests. **No control flow changes. No behaviour changes.**
WHAT:      Emit `floor_eligible_count` / `floor_eligible_notional` — the one-share
           floor's eligible SET — regardless of whether the floor flag is on.
WHY/DIR:   The strategy-104 enablement contract requires a **floor=OFF** dry-run
           proving the counters emit. The only floor counter that existed can only
           increment with the floor **ON**, so that evidence was structurally
           unobtainable and the flag could never be legitimately flipped.

EVIDENCE:  §4(b) block; model-specific fields filled and marked.

```
artifact:      src/renquant_pipeline/kernel/pipeline/task_selection.py (SizeAndEmitTask)
prod or exp:   prod — sizing runs on every live decision
existing data: strategy-104 configs/strategy_config.json has
               sizing.one_share_floor_enabled = false [VERIFIED, this session].
               grep across renquant-pipeline src/: floor_eligible_count 0 sites,
               floor_rescued_count 0, floor_rescued_notional 0; the only related
               counter is `one_share_floor_roundups`, incremented ONLY inside the
               deferred rescue pass, which the flag gates. [VERIFIED, this session]
best-known?:   NOT APPLICABLE as a model-variant comparison — no model, no score.
               As a fix: measures the eligible set WITHOUT enabling anything, which
               is the smallest change that unblocks the contract's prerequisite.
scope:         "this is task_selection.SizeAndEmitTask, PROD, a counter-only change;
               orders, block reasons and cash accounting are byte-identical in both
               flag states."
```

NEXT:      With this live, one production session answers "how much capital does
           integer rounding actually withhold" — the contract's own denominator.

## 1. The deadlock

`doc/progress/2026-07-12-one-share-floor-enablement.md` (strategy-104) requires, before
the flag may be flipped:

> *"at least one production dry-run with `floor=OFF` that proves the full
> pipeline→bundle→scorecard chain emits all 8 counters with correct values"*

`one_share_floor_roundups` increments **only inside the deferred rescue pass**, and that
pass is reachable only when `one_share_floor_enabled` is true. **A floor=OFF run emits
nothing**, so the prerequisite could not be satisfied by any run, ever.

Note which half was missing: the **numerator** (rescued) existed; the **denominator**
(eligible) did not — and the denominator is the half that is measurable with the feature
off.

## 2. What this adds

The eligible-set predicate is the rescue branch's own, **minus the flag**:

```
shares < 1  and  override_pct is None  and  max_pct > 0  and  price <= regime_cap × PV
```

so `floor_eligible_count` is a **superset** of `one_share_floor_roundups` — the deferred
pass can still decline a candidate for want of leftover cash. `floor_eligible_notional`
sums the one-share price, i.e. the capital the floor would have had a claim on.

## 3. Two controls and a mutation check

- **flag OFF behaviour is unchanged** — orders empty, block reason still
  `size_insufficient_cash`, `one_share_floor_roundups` still absent. A counter that also
  moved capital would be a trading change smuggled in as telemetry.
- **anti-vacuity ×2** — a name above the regime cap is not eligible, and a cheap name
  that sizes normally is not eligible. Without these the counter would just be
  `shares < 1`.

| mutation | result |
|---|---|
| drop the regime-cap test | **1 test fails** |
| drop the `max_pct > 0` / `override_pct` guard | **1 test fails** |

## 4. A vacuous test I wrote and then caught

The first version of the zero-conviction test used `panel_score=0.0, mu=0.0`. That
candidate is blocked **upstream** as `negative_raw_signal_no_long` and **never reaches
this branch**, so the test passed with the guard deleted — it asserted `count == 0` for a
name rejected for an unrelated reason. The mutation check is what exposed it: removing
the guard failed **zero** tests.

The replacement keeps the raw signal **positive** and drives `conviction_multiplier` to
exactly 0 via `min_mult=0.0` below the sizing floor, and asserts
`blocked == "size_insufficient_cash"` — that assertion is the proof the candidate
actually reached sizing. Deleting the guard now fails it.

## 5. Suite

`pytest tests/` → **1 failed, 2187 passed, 8 skipped**. The failure is
`test_xgboost_scorer_contract::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
**pre-existing** — reproduced on the unmodified base commit `[VERIFIED, this session]`.

## 6. What this does NOT do

It does not enable the floor, does not implement the other 6 contract metrics, and does
not touch the orchestrator scorecard integration the contract also requires. It removes
exactly one blocker: the prerequisite that could not be met by construction.

## Review round 1 — the counters were absent, not zero, exactly when that mattered

Codex: the metrics were created only inside the positive branch, so a floor-OFF run
with **no** eligible candidate emitted nothing for them — *"a zero-eligibility session
remains unobservable and can look like a missing integration"*.

That is fatal for this PR's purpose. The whole point is to satisfy the enablement
contract's dry-run prerequisite, and **absent** reads as *"the integration is missing"*
while **zero** reads as *"measured, and it was none"*. A counter that disappears
exactly when its value is the interesting observation cannot be enablement evidence.

`floor_eligible_count` and `floor_eligible_notional` are now `setdefault`-ed for
**every** sizing run, before the loop and independent of the flag.

## And the predicate was about to become a twin

Codex again: *"keep the eligibility predicate in one production helper shared with the
rescue branch, because this counter is being used as enablement evidence and a
duplicated predicate will silently drift from the behavior it is meant to measure."*

It was already duplicated — my measurement block recomputed the regime cap and the
`price <= cap + 1e-6` comparison that the rescue branch computes twelve lines later.
This repo keeps a registry for exactly that shape.

Extracted `floor_eligible(...)`, used by **both**. It is A-3 eligibility **minus the
flag**, which is what a flag-OFF count needs, and a test asserts the helper's source
never mentions `one_share_floor` so it cannot quietly acquire the flag later.
**A counter that has drifted from its subject is worse than no counter, because the
contract would then be satisfied by a number describing something else.**

`[VERIFIED — this session]` 32 pass. Load-bearing by injection, not assertion:
removing the unconditional init fails **3** tests; re-duplicating the predicate in the
rescue branch fails **1**; all pass again on restore.
