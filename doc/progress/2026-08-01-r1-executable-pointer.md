# GOAL-3 — R1's retirement condition #1: the repo now states which copy executes

**Date:** 2026-08-01 · `renquant-pipeline` · GOAL-3 (twin registry R1)

## Measured first: what GOAL-3 actually has

Before building anything I measured the twin registry's six retirement conditions against
the repos `[本次实测 2026-08-01]` — the lesson from two rounds where I rebuilt mechanisms
that already existed (twin-registry R5 on orch#694; `ack_expiry()` on orch#697, closed):

| condition | status |
|---|---|
| 5 — R8 canonical key + parity | **met** — `wf_gate_provenance.py`, presence-keying in `bundle_seal.py`, `gate_stamp_parity.py`, `test_twin_r8_canonical_gate_key.py` |
| 6 — R9 declared root, ambiguity is an error | **partial** — two tools refuse on AMBIGUOUS; nothing enforces it repo-wide |
| 2 — R1/R3 parity test | **absent for R1/R3** — `tests/test_twin_parity.py` covers the R0 sibling-repo pins, not the kernel-vs-public or trainer twins |
| **1 — executable pointer** | **absent** — a grep for a header naming the live copy returned nothing |
| 3 — single source for role assignment (R5/R6) | **absent** — orch#694 *detects* disagreement; it does not create a single source |
| 4 — reachability assertion (R7) | **MET** — `tests/test_wash_sale_cost_branch_reachability.py`; re-measured 2026-07-31: **6** call sites, **0** pass a real `expected_dollar_return` |

**2 of 6 met.** This PR does condition **1**, for R1.

> **CORRECTION 2026-08-01.** An earlier version of this table marked condition **4** as
> *absent* and totalled *"1 of 6"*. Both were wrong. My grep was
> `reachab|never reached|call site` over `tests/`, piped through `head -3`; it returned
> three alphabetically-first files and I read *"these are not it"* as *"it does not
> exist"*. **A truncated search result is not a negative result** — and I published a
> status table off one.
>
> Condition **2** moved the other way, from *"partial — coverage of R1/R3 not confirmed"*
> to **absent for R1/R3**: `tests/test_twin_parity.py` tests
> `scripts/check_twin_parity.py`, which pins **sibling-repo constants / functions / tax
> rules** (the R0 tripwires). It does not touch R1's kernel-vs-public twin or R3's three
> trainers.
>
> The measured facts elsewhere in this document — the `__init__.py` mapping, the missing
> pointer, `renquant-pipeline#222` — are unaffected.

## What R1 is

`renquant_pipeline/__init__.py:72` maps the public `VetoWeakBuysTask` to
`.panel_scoring` — **not** to the kernel implementation in
`kernel/panel_pipeline/job_panel_scoring.py`. Both are live, on different paths. The
hazard is one-directional and already paid: a fix landed in the kernel alone does not
reach the public export — `renquant-pipeline#222`, three kernel guards the public twin
never received.

`panel_scoring.py` already mentioned the kernel twin, but only in a comment attached to
**one constant** about keeping a score domain in lockstep. That is not a pointer to which
copy executes, and a reader landing in the kernel file learned nothing at all.

## What this adds

A module-level header on **both** files, each naming the other **by path**, stating which
one the public export resolves to and naming the cost. The direction that matters most is
the kernel side: a reader who lands there must learn, *there*, that importing the
documented public symbol does not run it.

**No behaviour change.** Docstrings and one new test file.

## Why it is a test and not just a comment

**A pointer is only a pointer while it is true.** `tests/test_r1_executable_pointer.py`
reads the actual mapping out of `__init__.py` and asserts it still points at
`.panel_scoring`. Re-point the export and the test fails — instead of leaving two headers
confidently asserting the opposite of what runs, which is R1's own failure mode reproduced
in the fix for R1.

6 tests: the mapping still holds and does **not** contain `kernel`; each header names the
other copy **by path**; both name the **cost** (`renquant-pipeline#222`), because a pointer
saying only "there are two" does not stop the failure; one grep phrase (`twin registry R1`)
finds **both** files, which is the registry's own wording — *"at a path a grep will hit"*;
and both headers **name this test**, so a reader editing a header is told what will catch
them and a reader breaking the test knows which headers to fix.

Suite: **2229 passed, 8 skipped, 1 failed**. That failure
(`test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`)
**pre-exists this branch** — verified by stash earlier in this session — and this branch
changes only docstrings and adds a test file, neither of which that scorer imports.

## Not done here

Conditions 3 and 4, and the R1 half of condition 2. Each has a different owner and blast
radius; R1's pointer was the one with no dependencies.
