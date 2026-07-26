# 2026-07-25 — Design PR: blend-objective shadow deployment

STATUS:    design only; no implementation, no config, no artifact
WHAT:      doc/design/2026-07-25-blend-shadow-deployment.md — shadow slot for the
           CONFIRMED blend objective via the existing shadow_scoring + #211
           health-record machinery; forward readout rule FROZEN pre-data
           (60-session INFO read; 120-session GATE read, block-bootstrap CI,
           winsorized guard, feed-integrity precondition; GO only submits to the
           normal WF-promote gate).
WHY/DIR:   Consequence step for the CONFIRMED blend-objective verdict
           (renquant-model#68/#73, orchestrator VERDICTS.md PROVISIONAL R1):
           the frozen prereg buys exactly a shadow design PR, nothing more.
           Advances the 5-step rollout in §5 of the design doc — this design
           gate first, then the model artifact PR, then the additive
           shadow-slot PR, then the orchestrator readout job — before any
           production surface is touched.
EVIDENCE:
  artifact:      renquant-model#68/#73 (frozen prereg + replayable results bundle);
                 orchestrator VERDICTS.md row (PROVISIONAL, R1)
  prod or exp:   design doc only — the prereg's frozen consequence for CONFIRMED
  existing data: shadow infra = shadow_scoring.py + #211 structured health record;
                 readout alarm pattern = GOAL-1 AC3
  best-known?:   forward evidence outranks the survivorship-panel historical result
                 by design; no production surface is touched at any step here
  scope:         "authorizes NOTHING except review of the frozen readout rule;
                 rollout steps 2-4 are separate reviewed PRs; machine landing keeps
                 the standard operator grant"
NEXT:      review; on merge -> model artifact PR, then the additive shadow-slot PR.
