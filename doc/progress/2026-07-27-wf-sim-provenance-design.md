# WF sim-time provenance contract — design PR

## STATUS
DESIGN only; no implementation, no production-path delta.

## WHAT
`doc/design/2026-07-27-wf-sim-provenance-contract.md`: per-(run,date)
append-only JSONL provenance record emitted at the `WalkForwardModelLoader`
boundary (the only seam where fold row + resolved artifact + digest co-exist),
admissibility-ledger digest grammar, sim-DB-independent durability, extraction
demoted to read + hard-error cross-check, tight sequencing behind
common#33/pipeline#214, prereg-before-rerun.

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
