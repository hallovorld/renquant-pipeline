# GOAL-3 — R1 retirement condition #2: a parity test that fails when the twins drift

**Date:** 2026-08-01 · `renquant-pipeline` · GOAL-3 (twin registry R1)

## Where GOAL-3 stands, re-measured

| # | condition | status |
|---|---|---|
| 1 | executable pointer | **met** — `renquant-pipeline#242`, merged |
| **2** | **parity test for R1 / R3** | **this PR, for R1** |
| 3 | single source for role assignment (R5/R6) | absent — orch#694 *detects* drift, does not source it |
| 4 | reachability assertion (R7) | met — `test_wash_sale_cost_branch_reachability.py` |
| 5 | canonical key + parity (R8) | met |
| 6 | declared root (R9) | partial — two tools refuse on AMBIGUOUS; no repo-wide enforcement |

**3 of 6 met** before this PR; R3's half of condition 2 is still open.

## What was actually missing

`tests/test_twin_parity.py` sounds like it covers this and does not: it tests
`scripts/check_twin_parity.py`, which pins **sibling-repo** constants, functions and tax
rules — the R0 tripwires. **R1's two copies had nothing.**

## What this pins, and what it deliberately does not

The copies are **not** meant to be identical — the public module is deliberately
lightweight and does not pull the kernel scoring stack in. Measured
`[本次实测 2026-08-01]`: **34** public top-level definitions, **61** kernel, **9 shared
names**.

So the test pins the surface they *do* share:

- the **lockstep constants** `RANK_SCORE_DOMAIN_RAW` / `_PROBABILITY` — which the public
  module's own comment already declared were kept in lockstep, with nothing enforcing it;
- the **shared symbol set**, so a task appearing in one copy and not the other is a diff
  here instead of a silence. A name added to **both** also fails, deliberately: growing the
  shared surface is exactly when *"which copy executes"* has to be re-answered.

Read by **AST, not by importing**: importing the kernel module pulls in the scoring stack,
which is what the public twin exists to avoid, and a parity test that forced the heavy
import would not run where it is most needed.

## Stated limits, as a test

`renquant-pipeline#222` — R1's recorded cost — was a **behavioural** divergence *inside* a
shared function: three guards that landed in the kernel and never reached the executing
copy. **No name-level pin can catch that.** A test asserts this file says so, because a
parity test that reads as stronger than it is would be R1's own defect one level up.

Two more scope guards: a constant **absent from both** copies must not pass as "in
lockstep", and the copies are asserted **not** identical — demanding identity would be
wrong about the design.

**8 tests.** Suite: **2237 passed, 8 skipped, 1 failed** — that failure
(`test_xgboost_scorer_contract.py`) **pre-exists this branch**, verified by stash earlier
in this session, and this branch adds only a test file.
