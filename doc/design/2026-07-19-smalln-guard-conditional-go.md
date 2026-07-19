# Amendment: conditional-GO for the small-n guard (N_shadow=10 is structurally unmeetable)

Date: 2026-07-19
Status: RFC amendment to the merged eligibility-ledger amendment
(`2026-07-18-smalln-guard-eligibility-ledger.md` §4). Drafted personally.
Does NOT flip any production key — production activation still requires the
operator's explicit on-record authorization (the original P0's requirement,
unchanged).

## A. The finding: the frozen N_shadow=10 bar cannot be met by any path

The §4 shadow verdict required GO evidence over **≥10 operative affected
sessions** (CLEAN small-n where the 0.50 absolute bound sits below the
mean+1σ floor, so relaxation changes the outcome). A full replay of the
DEPLOYED guard over ALL 43 live sessions of the entire governed-override era
(2026-04-23 → 2026-07-17, prod adaptive_mean_std floor, DB read-only)
established:

- **operative small-n sessions: 14** (n<12 AND relaxation-changing);
- **operative AND CLEAN: 2** (07-16, 07-17) — the structural maximum;
- **operative AND suppressed (fail-closed): 12** — every one a REAL
  failure-residue day (`kelly_zero:mu_none` ×5, `mu_le_min_edge` ×2,
  `regime_admission:failed:BULL_CALM` ×4, `defensive_non_bear` ×1).

Operative-CLEAN small-n days are structurally rare: they occur only when the
governed diagnostic-only override (pipeline#203) narrows the scan AND the
cross-section is clean. Sweeping the 29 sessions beyond the #543 corpus found
ZERO additional operative-CLEAN days. Live accrual runs at ~2 per 3 months of
override-era history, and the shadow arm's *quantile* floor accrues even
slower. **N=10 is unmeetable by replay and impractical by live accrual.**

N=10 was frozen on the assumption such days accrue at a workable rate; the
data shows they do not. This is a "frozen bar unmeetable with available
evidence" situation, not a guard defect — analogous to the G1 v5→v6
infeasibility catch. Continuing to demand N=10 leaves a VERIFIED-CORRECT fix
permanently shadow-dark (the deployed-but-dark anti-pattern GOAL-5 rejects).

## B. What the 14 operative sessions actually prove (the P0's real concern)

The independent-codex P0 that gated this guard was: *"the guard could relax
admission on scorer/data/feature FAILURE RESIDUE, exactly when the system
should stay fail-closed."* The 14-session replay addresses THAT hazard
directly and completely:

- **Safety (the P0 concern): OVER-proven.** All 12 real failure-residue days
  are correctly SUPPRESSED (the relax-only branch does NOT act); zero
  partition mislabels; zero cases where a failure day was wrongly CLEAN. The
  eligibility ledger's mass-balance + share-bound + failure-marker predicate
  fails closed on every real degraded day. This is STRONGER evidence for the
  P0 than "10 clean days" — it is 12 real adverse days, each correctly refused.
- **Usefulness (criterion iii): satisfied on the available operative-CLEAN
  set.** Both CLEAN days admit `{ATI, BWXT, EME}` (all μ>0), 100% ≥ the 70%
  bar, with every delta name traceable through the unchanged downstream gates
  (conviction μ / Kelly / QP) — no risk-gate bypass.
- **Detection: live backstop deployed.** The #549 sentinel fires LOUD on any
  small-n all-veto with n<12 regardless of guard config, so a post-activation
  regression pages immediately.

## C. Conditional-GO criterion (replaces the unmeetable N=10 volume bar)

Production activation is permitted under a CONDITIONAL-GO when ALL hold
(the safety-first reframing — the volume bar is replaced by an
adverse-day-coverage bar the P0 actually cares about):

1. **Adverse-day safety (the binding one):** over EVERY operative
   failure-residue session in the replay corpus (currently 12), the guard is
   SUPPRESSED (does not relax) — zero mislabel, proven by manual audit of
   each session's partition. A single mislabel → NO-GO.
2. **Correctness on CLEAN days:** every operative-CLEAN session admits only
   μ>0 names traceable through the downstream gates with zero risk-gate
   bypass (currently 2/2, {ATI,BWXT,EME}).
3. **Detection live:** the #549 small-n sentinel is deployed and fires on the
   synthetic + the real 07-17 all-veto (verified).
4. **Bounded blast radius:** the relax-only `min()` construction guarantees
   the guard can only WIDEN admission vs the status-quo floor, never narrow
   (one-sided, proven on all 14 + synthetic compressed sets), and admitted
   names still face every unchanged downstream capital gate.
5. **Operator on-record authorization** (unchanged from the original P0) —
   this amendment does NOT self-authorize; it defines the evidence standard,
   the operator makes the activation call with this verdict in hand.

A CONDITIONAL-GO is NOT a full GO: it is activation justified by
adverse-day-coverage + bounded-blast-radius + live-detection rather than
statistical volume, because the volume the frozen rule demanded is
structurally unavailable. The distinction is recorded in the verdict artifact.

## D. Honest alternative kept on the table

If the operator judges the 2-CLEAN-session usefulness evidence too thin
despite the over-proven safety, the guard stays shadow-only and the
recorded outcome is "CORRECT-BUT-UNACTIVATED (insufficient operative-CLEAN
volume; N=10 structurally unmeetable)" — a valid terminal state. This
amendment does not force activation; it makes the honest evidence and the
conditional-GO option explicit so the decision is informed.

## E. Acceptance

1. Codex adversarial review — especially attack whether the conditional-GO
   is a disguised parameter-walk (it is not: the SAFETY bar is raised, not
   lowered; only the volume bar is replaced, and only because it is
   structurally unmeetable) vs a genuine reframing.
2. The frozen verdict artifact (from the replay) attached, with the 14-session
   adverse-day-coverage table.
3. Operator on-record activation authorization BEFORE any production key-flip
   PR — the flip remains a separate reviewed config PR + pins bump.
