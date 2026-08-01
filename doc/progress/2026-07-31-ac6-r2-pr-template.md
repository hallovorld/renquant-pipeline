# GOAL-5 AC6 R2 — the override-path review surface lands in the repo that owns the gates

**Date:** 2026-07-31 · `renquant-pipeline` · GOAL-5 (P0) / AC6 R2, tracked in
`renquant-orchestrator`#564

## What this is

AC6's rollout has four rungs. R0/R1 (the canonical statement) exist: the design doc in
`renquant-orchestrator` and Universal Rule §7 in the umbrella architecture contract.
**R2 — the per-repo mechanical wiring — had landed only in `renquant-orchestrator`.**

This repo is where the rule actually bites: #564's grep put **85 files** here matching
`admission|_veto|sell_only|hard.?gate|fail.?closed`, the largest share in the programme.
The rule was canonical everywhere and present on the review surface of one repo that owns
comparatively little gate code.

So: a PR template, carrying the same item, **delegating to the canonical rule rather than
paraphrasing it** — a per-repo copy drifts from the rule it copies.

## What it is NOT, stated on the template itself

> *This checklist item is a review surface, not enforcement.*

Nothing mechanical rejects a run bundle missing override provenance today. That is not a
guess — it was measured this session in `renquant-orchestrator`#690: the shared
`LiveRunBundle` schema declares **7** fields and silently drops the rest, so a provenance
field added to that path would be **validated by nothing**
`[早前实测 2026-07-31, orch#690]`. Until R4 closes, this item and the reviewer reading it
*are* the gate, and the template says so in those words.

The alternative — shipping the item quietly and letting it read as a mechanical guarantee
— is the failure already on the register: scaffolding whose appearance of control exceeds
its actual control.

## Why a checklist item gets a test

A PR-template item is the weakest control in the programme: nothing runs it, and it can be
removed in a one-line diff no test notices. `tests/test_ac6_pr_template_contract.py`
pins that it exists and says what it must:

- all **three** required properties are named — *identity*, *expiry*, *binding*
  (two of three yields a checklist that passes on a gate nobody can lift, or one nobody
  can find);
- a **hard gate is defined** rather than assumed, or every author may decide the item is
  N/A;
- **"Temporary" is refused as an expiry** — the containment-protocol lesson, in the words
  it was learned in;
- it **points at the canonical rule**, not a local paraphrase;
- it **states that this is not enforcement**.

7 tests.

## Scope and what remains

- R2 is now landed in **2 of 4** repos: `renquant-orchestrator` and this one.
  `renquant-execution` (order-level blocks) and `renquant-strategy-104` (holds the
  threshold *config values* that pipeline gates read) remain.
- **R4 is blocked, not pending** — closing it needs either `extra="forbid"` plus declaring
  the real fields, or a purpose-built daily-bundle contract. That is a shared-contract
  change across repos and is not mine to take alone.

Suite: **2218 passed, 8 skipped, 1 failed**. The one failure,
`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
**pre-exists this branch** — verified by stash earlier this session, and this branch
changes only a markdown file and adds a new test file, neither of which the scorer imports.
