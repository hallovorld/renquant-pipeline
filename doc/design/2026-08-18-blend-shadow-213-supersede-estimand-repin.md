# Superseding design: re-pin the #213 blend-shadow estimand before it is unfixable

STATUS: **superseding design PR (docs only)** amending
`doc/design/2026-07-25-blend-shadow-deployment.md` per its own §4 anti-drift rule
("any amendment = superseding design PR"). DATE: 2026-08-18. Triggered by the
2026-08-17 forward-shadow audit — committed alongside this design as
`doc/research/2026-08-17-blend-shadow-forward-audit.md` (each finding lists its
primary source there).

## 1. Why now — the defect that voids the readout if left until gate time

#213's primary statistic compares the blend against "prod", and never pinned prod's
identity. On **2026-08-04 prod changed identity** (active_scorer `panel_ltr_xgboost` →
the momentum z-blend; recorded `panel_score` scale jumped ~0.30→~2.9). Consequence:
- ledger rows 07-27..08-03 (6 sessions) measure the **certified** contrast —
  z(panel-xgb)+z(clf) vs panel-xgb (the model#76 construction, +0.0687/60d CI90
  [+0.0156,+0.1269], two disjoint seed draws);
- rows 08-04.. (10 sessions and ALL future ones) measure z(z(xgb)+z(mom))+z(clf) vs
  z(xgb)+z(mom) — **a construction never screened, never preregistered**.
The eventual GATE@120 would pool two estimands, ~95% the unregistered one. Fixing now
costs a re-pin over 16 sessions; fixing at gate time (~2027-04) voids 120.

## 2. The re-pin (supersedes #213 §2's statistic definition; rules otherwise unchanged)

**Estimand re-pinned to the certified construction, retroactively and prospectively:**
- prod arm = top-10 by the **pure panel-xgb score**;
- blend arm = top-10 by z(pure panel-xgb) + z(clf), per date, unweighted (unchanged);
- both arms computed FROM PER-NAME SCORES (pure panel scores remain recorded per session
  in the MLflow comparison tables / lane records — the audit verified per-name
  availability), never from `candidate_scores.panel_score` whose identity floats with
  the served config.
- The readout records, per session, the panel-artifact sha actually scored (ordinary
  RFC#210 freshness churn of the panel vintage is ACCEPTED, as the original design
  implicitly did; a vintage ROTATION is logged, never silently pooled away).
- All 16 existing ledger rows are RECOMPUTED offline under this definition in the
  implementation PR, with the recomputation script + before/after table committed; rows
  whose per-name sources cannot support recomputation are dropped and counted (feed
  integrity, not discretion).

## 3. Horizon amendment — ratified, with the governance record closed

The 07-29 fwd_20d→fwd_60d + MATURITY 21→61 amendment (orch `690df5da`) is ratified as
part of THIS superseding PR — it is substantively correct (both models are fwd_60d
recipes; the certified +0.0687 is a 60d number) but was procedurally undocumented in
this repo. **Operator attestation required on this PR**: the amendment doc records it as
an operator decision "not independently checkable"; the operator's approving comment
here becomes that record. Revised calendar under 61-td maturity (from the audit,
trading-day arithmetic): INFO@60 ≈ **2027-01-15**, GATE@120 ≈ **2027-04-14**.

## 4. Pre-committed sensitivity treatment (before any sign is knowable)

Ledger rows **2026-07-27/28/29** carry unestablished clf-table attribution (the
locator's identity guard first executed 07-29; orch `0dcd5406` deliberately recorded
"unestablished provenance"). Pre-commit, now: the **decisive GATE read EXCLUDES** these
three rows; INFO and GATE reads also REPORT the including variant alongside. Frozen
here so no one chooses after seeing signs.

## 5. Maturation-calendar guard

`ticker_forward_returns`' session calendar contains 6 phantom weekend dates and one
missing weekday in its pre-window history. Before the first realization (~2026-10-21),
the readout's `_aged_dates` must filter its calendar to NYSE trading days and alarm on
any post-07-27 phantom — a guard, not a data rewrite.

## 6. Unchanged

Reads only at 60/120/+60N matured sessions (no peeking — this PR reads nothing);
the GATE rule (block-bootstrap 90% CI, GO→normal WF-promote submission / KILL /
extend-to-240-INCONCLUSIVE); winsorized guard; <5% silent-skip precondition; blend
composition and its no-weights rule; "GO authorizes only a submission to the normal
weekly promote gate, never a capital decision."

## 7. Implementation (separate PR after approval)

Readout job changes (per-name-score arms + sha recording + calendar guard) +
the 16-row offline recomputation with committed before/after evidence. Owner:
the readout job lives in the orchestrator ops surface; the rule lives here — the impl
PR lands in renquant-orchestrator referencing this design; deploy to the run surface is
operator-gated as always.
