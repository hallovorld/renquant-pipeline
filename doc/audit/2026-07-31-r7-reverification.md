# #623 row R7, re-verified: no measured call site reaches the cost-aware branch — and the call sites doubled

**Bottom line `[本次实测 2026-07-31]`.** The registry recorded R7 as *"branch (a) validates
nothing in production — no caller passes `expected_dollar_return`"*, measured at **3**
call sites in this repository. There are now **6** in `src/`, and across **10 checkouts**
scanned — every RenQuant repo on this machine plus the umbrella — **zero** of **21**
calls pass a real value.

| | registry (2026-07-30) | re-measured (2026-07-31) |
|---|---:|---:|
| call sites in this repo's `src/` | 3 | **6** |
| call sites in *any* scanned checkout | — | **21** (15 here, 6 in the umbrella's twin copy) |
| passing a real `expected_dollar_return` | 0 | **0** |
| naming the parameter at all | — | **2**, both passing `None` explicitly |

The site that names it here is `kernel/pipeline/task_candidates.py:81`:
`expected_dollar_return=None,   # μ̂ not yet known at this stage`.

> **The row is stale in the direction of understating the problem.** The pattern spread
> to twice as many call sites in this repo while the defect persisted. Branch (a) — the
> cost-aware half of a cost-aware gate — is not reached from any call site measured.

## Scope: what "no caller" covers, corrected after review

The first version of this document said the branch *"has never executed in production"*.
**That claim was wider than the measurement.** `renquant_pipeline` is a shared package;
a scan of this repository's `src/` cannot see a consumer that imports it. Reviewed
`[codex on #239]`: *"this shared package can be called by downstream repositories or
installed consumers, which the scan does not cover."*

So the census was re-run across every consumer, with the tool committed alongside it:

```
python3 tools/call_site_inventory.py \
    --function is_wash_sale_blocked_with_cost --kwarg expected_dollar_return \
    --root ../renquant-pipeline    --root ../renquant-orchestrator \
    --root ../renquant-common      --root ../renquant-execution \
    --root ../renquant-backtesting --root ../renquant-strategy-104 \
    --root ../renquant-model       --root ../renquant-base-data \
    --root ../renquant-artifacts   --root ../RenQuant
```

`[VERIFIED — that command, 2026-07-31]`: `calls: 21  [real_value=0  explicit_none=2
absent=19]`, over these HEADs:

| root | HEAD | calls |
|---|---|---:|
| `renquant-pipeline` | `a14dad1` | 15 (6 in `src/`, 9 in tests) |
| `RenQuant` (umbrella) | `3f4e3d6` | 6 — all in `backtesting/renquant_104/`, the **twin copy** of this kernel |
| `renquant-orchestrator` | `e447aaa4` | **0** |
| `renquant-common` | `19c8f5b` | **0** |
| `renquant-execution` | `69f01b1` | **0** |
| `renquant-backtesting` | `1788104` | **0** |
| `renquant-strategy-104` | `4ee1a10` | **0** |
| `renquant-model` | `365266c` | **0** |
| `renquant-base-data` | `571e10a` | **0** |
| `renquant-artifacts` | `027d8a9` | **0** |

**No repo outside `renquant-pipeline` calls this function at all.** The six calls in the
umbrella are its vendored copy of this same kernel, so they are the defect duplicated,
not a consumer supplying the parameter.

### The AST pass's blind spot, checked rather than assumed

A name reached through `getattr`/a dispatch table is invisible to an AST call scan, so
"21 calls" would silently be a lower bound. The tool therefore reports **string
mentions** separately: **18**, of which **14 are docstrings** and **4 are grep-based
audit assertions in the umbrella's own tests** (`assert "is_wash_sale_blocked_with_cost"
in body`). `[VERIFIED — classified by AST: a mention is a docstring iff it is the first
statement of its module/class/function]` **Zero dynamic dispatch**, so the call count
stands for every call in the scanned roots.

### What is still NOT covered

- an **installed** copy of the package (a wheel in a venv, a private fork, a checkout on
  another machine) — outside every root, and outside what static scanning can reach;
- **runtime** behaviour: this measures reachability from source, not that the enclosing
  functions ran. The claim is "no call site supplies the parameter", not "the process
  never entered the function".

## Why this is the registry's own thesis

#623 exists because *"nothing in the repo tells you which copy executes."* R7 is the
same failure inside a single function: the code reads as cost-aware, and the branch that
makes it so is not reached. A reader auditing the call sites today would find **twice**
the surface the registry describes — which is exactly how a hand-maintained registry
stops being a description of the system. And the cross-repo census makes the point
twice: the second-largest concentration of these call sites is a **copy** of this kernel
in the umbrella, which the registry's R1 row is also about.

## What landed

A test that asserts the count and the reachability **by AST**, not by grep. Every call
spans several lines, and a line-oriented regex over multi-line call signatures is how
this programme has published wrong counts before.

The next caller added without the parameter now **fails a test** instead of quietly
extending an unreached branch. If someone wires a real `expected_dollar_return`, the
zero-assertion fails and the registry row is retired deliberately rather than forgotten.

`tools/call_site_inventory.py` makes the census above **rerunnable** — it takes roots,
prints the roots it used, and separates absent / explicit-`None` / real-value. It is
tested against a **controlled fixture tree**, not against this repo and not against the
operator's other checkouts: a test whose subject is the machine's source goes red when
that source legitimately changes and passes for the wrong reason on a machine that has
none of it.

## Not claimed

**Not that pipeline#227 failed.** #227 addressed the cost-aware *gate*; it did not wire
`μ̂` into the call sites, and nothing in this measurement says it was supposed to.

**Not re-verified this round:** rows R2–R6. I attempted R5 first and stopped — I was
guessing at config paths and key names, which is precisely the error class this registry
catalogues. Re-verifying those requires #623's own file references, not my
reconstruction of them.

Tests: 4 on the reachability claim (including an anti-vacuity control that exactly one
site in `src/` names the parameter) and 7 on the inventory tool.
