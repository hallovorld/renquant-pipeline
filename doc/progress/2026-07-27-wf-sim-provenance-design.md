# WF sim-time provenance contract — design PR

## STATUS
DESIGN only; no implementation, no production-path delta. Round 2 after
codex CHANGES_REQUESTED (two P1 evidence-contract gaps), both addressed.

## WHAT
`doc/design/2026-07-27-wf-sim-provenance-contract.md`: per-(run,date)
append-only JSONL provenance record emitted at the `WalkForwardModelLoader`
boundary (the only seam where fold row + resolved artifact + digest co-exist),
admissibility-ledger digest grammar, sim-DB-independent durability, extraction
demoted to read + hard-error cross-check, tight sequencing behind
common#33/pipeline#214 (both MERGED 2026-07-27), prereg-before-rerun.
Round-2 revisions per codex P1s: (a) TWO-PHASE records — `fold_resolved`
(loader boundary) + `score_committed` (post-INSERT commit point) with
`score_observation_key`, canonical `score_payload_digest`, `n_rows`,
artifact-digest echo; extraction rejects orphaned/duplicate/incomplete/
mismatched pairs; (b) `score_timestamp` = the SIMULATED session decision
instant (decision_schedule convention, America/New_York ISO-8601) with the
enforced PIT invariant `input_watermark <= score_timestamp`;
`emitted_at_utc` demoted to audit-only in both record kinds.

## WHY-DIR
Root unblock demanded by codex on model#64/#65/#66: `score_distribution`
records only the scorer family, so Phase-A evidence reconstructs fold/digest
post-hoc — inadmissible. Emit-at-loader covers every sim entry point
(run_sim_104 / dump_walkforward_sim_metrics / weekly_wf_promote) with one
hook and zero live-surface delta (sink constructed by sim drivers only).

## EVIDENCE
Design doc §1 cites exact code facts verified in-tree 2026-07-27:
`decision_trace.active_scorer_identity` family-only; `_SIM_RESET_TABLES`
truncation; `_scorer_claim_for_entry` already hashing fold artifacts;
`pipeline_runs` columns available for mirror; `pit_parity_ledger` multi-repo
pin capture as the revision-pins precedent. No behavior claims made.

## NEXT
Codex review of this design → implementation PRs per §3 sequencing →
model#65/#66 rebase → pre-registered multi-seed reruns.
