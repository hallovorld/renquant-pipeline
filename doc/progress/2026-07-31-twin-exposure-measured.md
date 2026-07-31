# GOAL-3 — the twin's exposure, measured: the CONTRACT CHECK runs the unguarded copy

**Date:** 2026-07-31 · `renquant-pipeline` · GOAL-3 · follow-up to #221 / #222

**Guidance only. No implementation, no behaviour change.** GOAL-3's charter is the
violation registry plus remediation guidance; this document supplies the measurement
that #222's own closing question asked for.

## Bottom line

#222 asked, at the end: *"whether the twin should keep existing at all, or whether the
public export should simply point at the kernel."* Measured, that question now has an
answer that patching a 4th guard would have hidden.

**The live buy path uses the kernel. The thing that runs the twin is the contract
fixture — whose stated purpose is to prove the wiring through "the real subrepo package
contracts."** So the check written to validate the contract validates the *unguarded*
implementation, while production runs the guarded one.

That is [[guards-that-validate-the-wrong-object]] at the level of a whole module.

## The measurements

All at `origin/main` = `aa3dffb` `[VERIFIED — this session]`.

| quantity | kernel | twin |
|---|---:|---:|
| `job_panel_scoring.py` / `panel_scoring.py`, lines | **4 275** | **980** |
| fail-closed reason strings | **45** | **3** |
| `soft_check_score_series` / `panel_score_collapsed` | 5 / 1 | **0 / 0** |
| `config_consistency` / `assert_consistent` | 7 / 2 | **0 / 0** |

So #222's three gaps are **still open**, and they are not the whole story: the guard
counts differ by **45 vs 3**.

**Every one of the 7 public names that resolve to the twin has a same-named kernel
counterpart** `[VERIFIED — grep '^class <name>' in both trees]`:
`ApplyGlobalCalibrationTask`, `ApplyScoresTask`, `BuildFeatureMatrixTask`,
`LoadScorerTask`, `PanelScoringJob`, `RegimeModelAdmissionTask`, `VetoWeakBuysTask`.

## Who actually reaches the twin

`renquant_pipeline/__init__.py` maps those 7 names to `.panel_scoring`. Consumers of the
**public top-level export** outside the pipeline repo `[VERIFIED — grep across all sibling
repos, excluding .git and agent worktrees]`:

| repo | public-export imports | of which reach the twin |
|---|---:|---|
| `renquant-orchestrator` | 18 | `src/renquant_orchestrator/contract_fixture.py:13` (`PanelScoringJob, SelectionJob`), plus tests |
| `renquant-strategy-104` | 0 | — |
| `renquant-execution` | 0 | — |
| `renquant-model` | 1 | — |
| `renquant-backtesting` | 2 | — |

`contract_fixture.py`'s own docstring: *"a small no-network fixture that proves the
orchestrator can wire training, inference, execution, backtesting, and run bundle
persistence through the **real subrepo package contracts**."*

It is registered as job `daily_contract_fixture`
(`scheduled_jobs.py:121`, `job_runner.py:67`, CLI `run-job daily_contract_fixture`) but
is **not** in `ops/launchd_manifest.json` and **not** in `launchctl list` on this machine
`[VERIFIED — this session]`. So it is a defined job that is not currently scheduled here.

The live daily run resolves scoring through the **kernel**: today's decision log emits
`kernel.panel_pipeline.scoring: VetoWeakBuysTask: dropped 62 candidate(s)…`
`[VERIFIED — this session, 2026-07-30 14:08 run]`.

## Why this changes the remediation, not just its priority

The natural next step after #221 and #222 is *add the missing guard to the twin*. That is
the third time, and it is the enumerating fix: it closes the instance and leaves the
mechanism — a second implementation, 4× smaller, with 3 refusals against 45, sitting
behind the **public** name.

The measurement says the cost of the alternative is small and shrinking:

- **no production caller** outside the pipeline reaches the twin for scoring;
- the **only** non-test caller is a fixture that explicitly wants the real contract;
- all 7 exported names already exist in the kernel.

**Recommendation for review to accept or reject:** re-point the 7 `.panel_scoring`
exports at the kernel and delete the twin, rather than adding a 4th guard. Two things
must be settled first, and neither is settled here:

1. **Signature compatibility per name.** Same class name is not the same constructor or
   the same `run()` contract. Each of the 7 needs a signature diff before any swap.
2. **What the fixture is for.** If `daily_contract_fixture` is deliberately a
   *lightweight* wiring smoke test, pointing it at a 4 275-line kernel path may be a
   different job than the one that exists. That is a design call, not a mechanical one.

## What is NOT claimed

- Not claimed that the twin has mis-traded. It is not on the live buy path.
- Not claimed that the 3 gaps in #222 are harmless — they are open, and the fixture that
  exercises them is the one whose job is to prove the contract holds.
- No signature diff was run. Item 1 above is the next measurement, not a finding.
