# #623 row R7, re-verified: the cost-aware branch is still unreachable — and the call sites doubled

**Bottom line `[本次实测 2026-07-31]`.** The registry recorded R7 as *"branch (a) validates
nothing in production — no caller passes `expected_dollar_return`"*, measured at **3**
call sites. There are now **6**, and **still zero** pass a real value.

| | registry (2026-07-30) | re-measured (2026-07-31) |
|---|---:|---:|
| call sites in `src/` | 3 | **6** |
| passing a real `expected_dollar_return` | 0 | **0** |
| naming the parameter at all | — | **1**, passing `None` explicitly |

The one site that names it is `kernel/pipeline/task_candidates.py:90`:
`expected_dollar_return=None,   # μ̂ not yet known at this stage`.

> **The row is stale in the direction of understating the problem.** The pattern spread
> to twice as many call sites while the defect persisted. Branch (a) — the cost-aware
> half of a cost-aware gate — has never executed in production.

## Why this is the registry's own thesis

#623 exists because *"nothing in the repo tells you which copy executes."* R7 is the
same failure inside a single function: the code reads as cost-aware, and the branch that
makes it so is dead. A reader auditing the call sites today would find **twice** the
surface the registry describes — which is exactly how a hand-maintained registry stops
being a description of the system.

## What landed

A test that asserts the count and the reachability **by AST**, not by grep. Every call
spans several lines, and a line-oriented regex over multi-line call signatures is how
this programme has published wrong counts before.

The next caller added without the parameter now **fails a test** instead of quietly
extending an unreachable branch. If someone wires a real `expected_dollar_return`, the
zero-assertion fails and the registry row is retired deliberately rather than forgotten.

## Not claimed

**Not that pipeline#227 failed.** #227 addressed the cost-aware *gate*; it did not wire
`μ̂` into the call sites, and nothing in this measurement says it was supposed to. What
is claimed is narrower and checkable: **as of today the branch is unreachable, across
6 call sites.**

**Not re-verified this round:** rows R2–R6. I attempted R5 first and stopped — I was
guessing at config paths and key names, which is precisely the error class this registry
catalogues. Re-verifying those requires #623's own file references, not my
reconstruction of them.

Tests: 4, including an anti-vacuity control that exactly one site names the parameter.
