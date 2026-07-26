# 2026-07-25 — Design PR: blend-objective shadow deployment

STATUS:    PARKED — design only; no implementation, no config, no artifact;
           not authorized for rollout (see WHY/DIR)
WHAT:      doc/design/2026-07-25-blend-shadow-deployment.md — captures a
           reusable shadow-readout design (60-session INFO read; 120-session
           GATE read, block-bootstrap CI, winsorized guard, feed-integrity
           precondition; GO only submits to the normal WF-promote gate) for
           the blend objective via the existing shadow_scoring + #211
           health-record machinery. No rollout step is currently authorized.
WHY/DIR:   Originally framed as the consequence step for a CONFIRMED
           blend-objective verdict (renquant-model#68/#70). That verdict is
           superseded: renquant-model#73 (replayable results bundle) corrects
           the numbers to +0.0602/60d, CI90 [+0.0116,+0.1155], 9/10 seeds,
           winsorized guard +0.0125, downgrades PR standing to
           EXPLORATORY/PROVISIONAL (the exact `blend` construction was never
           individually screened), and states verbatim "Consequence:
           WITHDRAWN. No shadow-design PR ... authorized by this PR."
           Orchestrator VERDICTS.md's blend row was added then reverted in
           the same round (`79493a11`). Re-add / reopening condition:
           renquant-model#73's own prescription — a pre-registered screen of
           the exact blend construction, then a re-frozen confirmatory
           prereg citing it. This PR does not advance the §5 rollout order
           until that prereg lands.
EVIDENCE:
  artifact:      renquant-model#68 (frozen prereg, MERGED) / #70 (CLOSED,
                 superseded) / #73 (results v2, MERGED, replayable bundle,
                 the current evidentiary artifact)
  prod or exp:   design doc only — no shadow-design PR is currently authorized
  existing data: shadow infra = shadow_scoring.py + #211 structured health
                 record; readout alarm pattern = GOAL-1 AC3; orchestrator
                 commit 79493a11 reverted the VERDICTS.md re-add for the same
                 reason this design is parked
  best-known?:   renquant-model#73 is the current best-known (and only
                 evidentiary) artifact for this line; it is EXPLORATORY/
                 PROVISIONAL, not CONFIRMED
  scope:         "authorizes NOTHING beyond this parked reference doc — the
                 readout-rule mechanism in §2 is preserved for reuse once the
                 reopening condition (screened blend + re-frozen prereg) is
                 met; §5 steps 2-5 (gate + rollout) remain un-triggered"
NEXT:      wait on renquant-model: pre-registered screen of the exact blend
           construction, then a re-frozen confirmatory prereg. Only then do
           §5 steps 3-5 (the actual rollout PRs) become actionable; this
           design doc itself is already an archived parked reference once
           merged, not a pending merge decision.
