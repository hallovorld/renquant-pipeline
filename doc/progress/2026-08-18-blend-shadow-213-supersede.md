# #213 superseding design — estimand re-pin (doc only)

STATUS:    superseding design PR per #213 §4's own anti-drift rule. Docs only.

WHAT:      `doc/design/2026-08-18-blend-shadow-213-supersede-estimand-repin.md`:
           re-pins the forward-shadow estimand to the CERTIFIED construction (both arms
           from per-name pure panel-xgb scores; retroactive 16-row recomputation with
           committed evidence), ratifies the 07-29 horizon amendment with the operator
           attestation slot, pre-commits the 3-row sensitivity treatment before signs
           are knowable, adds the maturation-calendar NYSE guard. Gate rules unchanged;
           nothing is read.

WHY/DIR:   2026-08-17 audit [all VERIFIED]: prod changed identity 08-04 (momentum
           z-blend became the recorded panel_score), so post-08-04 ledger rows measure
           a never-preregistered contrast — ~95% of the eventual GATE@120 would be the
           unregistered estimand. Fix now = re-pin over 16 sessions; fix at gate
           (~2027-04) = void 120.

EVIDENCE:
  artifact:      the superseding design + this doc.
  prod or exp:   neither — design only; no read performed (no-peeking intact), no live
                 change.
  existing data: [VERIFIED — committed audit record
                 `doc/research/2026-08-17-blend-shadow-forward-audit.md`, added in THIS
                 PR; each claim lists its primary source there] 16/16 sessions, 0 gaps,
                 0% silent-skip (ledger.jsonl direct read); clf sha byte-stable since
                 07-28 (shadow_scorer_health.jsonl + on-disk hash); 0 matured sessions;
                 INFO@60 ≈ 2027-01-15, GATE@120 ≈ 2027-04-14 (trading-day arithmetic);
                 08-04 baseline identity flip (SQL pre/post on runs.alpaca.db
                 candidate_scores: scale 0.30→2.9, picks match momentum-blend top-10);
                 07-27..29 provenance gap per orchestrator commit 0dcd5406's own body.
  best-known?:   yes — the re-pin PRESERVES the model#76 certification's subject
                 instead of silently substituting a new one; recomputation is from
                 recorded per-name scores (no re-scoring, no hindsight); the sensitivity
                 rule is frozen before any sign exists; the alternative (re-freeze as
                 "blend-vs-whatever-prod-serves") was rejected because it abandons the
                 certification and pools estimands.
  scope:         "amends the #213 statistic's PROD-ARM DEFINITION and adds guards;
                 changes NO gate rule, NO read schedule, NO serving behavior. The impl
                 PR (orchestrator readout job + 16-row recompute) follows approval;
                 run-surface deploy operator-gated. Requires operator attestation of
                 the 07-29 horizon amendment on this PR."

TESTS:     none — doc-only PR.

NEXT:      codex review + OPERATOR attestation (horizon amendment) → impl PR in
           renquant-orchestrator → operator-gated deploy. First realization ~2026-10-21;
           calendar guard must land before it.
