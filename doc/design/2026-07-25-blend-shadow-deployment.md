# Design — shadow deployment of the blend objective (PARKED, no production change)

STATUS: PARKED — not authorized for rollout/activation. This document itself
is an inert, additive reference (no code/config/artifact change — see §3) and
is authorized to merge as a parked record; §5 steps 2-5 (the gate and the
actual rollout) stay blocked until the reopening condition below is met.
renquant-model#73 (the replayable
results bundle superseding #70) corrects the harvest statistic to
+0.0602/60d clean top-10 spread, CI90 [+0.0116,+0.1155], 9/10 seeds,
winsorized-±50% guard +0.0125 — but independent of the numbers, its own
body downgrades PR standing to **EXPLORATORY / PROVISIONAL** (the frozen
prereg #68 screened the component arms individually, never the exact
`blend` construction under test) and states verbatim: *"Consequence:
WITHDRAWN. No shadow-design PR and no orchestrator ledger VERDICTS row
re-add are authorized by this PR."* Orchestrator `VERDICTS.md` reflects
this — the row this design cited was added then reverted in the same PR
round (`79493a11`, "model#73 withdraws re-add authorization").
Reopening condition: a pre-registered screen of the exact blend
construction (committed evidence), then a re-frozen confirmatory prereg
citing that screen. Until then this design captures the readout-rule
mechanism for reuse but authorizes no rollout step in §5.

## 1. What ships (three small pieces, all additive)

1. **Artifact** (renquant-model): `panel-clf.top-decile.fwd60.json` — the
   binary top-decile-membership classifier from the confirmatory executor,
   productionized by the existing artifact-contract path (v3 schema, config
   fingerprint, provenance stamps). The blend WEIGHTS are not an artifact:
   blend = z(prod) + z(clf) per date, fixed, from the frozen prereg.
2. **Shadow slot** (this repo): the classifier runs as a SHADOW scorer via
   the existing `shadow_scoring.py` machinery (scores recorded, no orders),
   emitting the #211 structured health record each session. No new runtime
   machinery: production score and shadow score are both already recorded;
   the blend is computed OFFLINE in the readout job.
3. **Readout job** (orchestrator ops, later PR): daily job joins recorded
   (prod, clf) scores, computes the blend, and appends per-session
   `spread_prod` / `spread_blend` on realized forward returns to a readout
   ledger. Alarm if the shadow feed goes silent (GOAL-1 AC3 pattern).

## 2. The frozen forward readout rule

- **Primary statistic**: paired per-session difference
  `top10_spread(blend) − top10_spread(prod)` on realized `fwd_20d` returns
  (20d chosen for maturation speed; `fwd_60d` secondary).
- **Maturation**: first read at **60 matured sessions** (~3 independent 20d
  blocks — deliberately an INFO read, not a gate); the GATE read at
  **120 matured sessions** (~6 blocks).
- **GATE rule (frozen now, before any data):** at 120 sessions, moving-block
  bootstrap (block 20) 90% CI on the mean paired difference:
  - lower bound > 0 → **GO to a WF-promote submission** (the blend recipe
    enters the NORMAL weekly promote gate — this design never bypasses it);
  - upper bound < 0 → **KILL** the line; register the reversal;
  - otherwise → extend one 60-session block at a time to a 240-session cap,
    then close INCONCLUSIVE (no-peeking between scheduled reads).
- **Guards carried from the prereg**: winsorized-±50% paired diff ≥ 0 at the
  gate read (anti-lottery); shadow-health record must show < 5% silent-skip
  sessions or the read is postponed (feed-integrity precondition).

## 3. What this explicitly does NOT do

- No change to `strategy_config.json`, the production scorer, or any pin.
- No capital decision: even a GO only submits the recipe to the standard
  WF-promote gate (model#67-class evidence goes there, not here).
- No new alpha claim: the historical +24% is survivorship-panel evidence;
  this design exists precisely because forward evidence outranks it.

## 4. Failure modes designed against

| risk | countermeasure |
|---|---|
| shadow feed dies silently (2026-07-15 class) | #211 health record + readout-job alarm on missing sessions (GOAL-1 AC3) |
| readout peeking / rule drift | rule frozen here, pre-data; reads only at 60/120/+60N; any amendment = superseding design PR |
| clf artifact drifts from prod recipe | `StaleSpecialistArtifact`-style fingerprint check at load (feature_cols ⊆ prod's; config fingerprint bound) |
| survivorship optimism carried forward | forward sessions are point-in-time by construction — that is the point |

## 5. Rollout order

1. This design doc merges to `main` as a parked reference (review may amend
   §2 thresholds — that is the review's job; after merge they freeze). This
   step makes no code/config/artifact change (§3) and is NOT gated — it only
   records the mechanism for reuse.
2. **Gate (not yet met):** pre-registered screen of the exact `blend`
   construction, then a re-frozen confirmatory prereg citing that screen
   (renquant-model#73's reopening condition). Steps 3-5 below stay BLOCKED
   until that prereg exists — rollout/activation remains unauthorized.
3. model PR: artifact training script + provenance (mechanical port of the
   confirmatory executor's classifier).
4. this-repo PR: shadow-slot config entry (additive) + health-record name.
5. orchestrator PR: readout job + ledger + launchd manifest entry (goes
   through the run-surface review path; machine landing needs the standard
   operator grant).
6. First INFO read ~3 months after activation; GATE read ~6 months.
