# 2026-08-30 — CI is red for every pipeline PR: scs 3.3.0 kills the interpreter on the first infeasible QP

**Bottom line:** `renquant-pipeline` CI has been failing since ~17:35Z today on
**every** branch, including `main`'s own unchanged commit. The suite dies
mid-run with `pytest` exit 2 and no traceback, always after exactly 183 test
outcomes — the next item is the first **infeasible** QP solve, the only path
that falls through the solver chain to **SCS**. `scs 3.3.0` was published to
PyPI at 2026-08-30T16:08:17Z and CI installs solvers unpinned. This PR pins
`scs<3.3.0` in the CI install step and nothing else. **The decision it forces:
none — it unblocks #303/#304/#305, whose diffs are not the cause.**
[VERIFIED — evidence below]

## The failure is not any PR's diff

`main` @ `afb73626` was green in run `33246639677` (2026-08-29T09:56Z).
Re-running **that same run** today (attempt 2, 18:31Z) reproduced the failure
byte-for-byte:

```
python3 -m pytest -q
........................................................................ [  2%]
........................................................................ [  5%]
make: *** [Makefile:18: test] Error 2
.......................................
```

Same signature on all three open PRs — `#303` (run 33325659879), `#304`
(33326007285), `#305` (33326022183) — including `#305`, which shares no code
with the other two. Identical stop point, identical exit code.
[VERIFIED — GitHub Actions logs]

## What changed under the repo

Diff of the `Successfully installed …` line, green attempt vs red attempt of
the *same commit*:

| package | green (08-29) | red (08-30) |
|---|---|---|
| patsy | 1.0.2 | 1.0.3 |
| **scs** | **3.2.11** | **3.3.0** |
| wrapt | 2.3.0 | 2.4.0 |

Nothing else moved — 50 of 53 entries are identical, and the sibling
checkouts/`-e` installs report the same versions. `scs 3.3.0` upload time is
2026-08-30T16:08:17Z (PyPI JSON), **88 minutes before the first red run**.
[VERIFIED]

## Why scs and not patsy/wrapt

183 outcome characters are printed (72 + 72 + 39), all `.` — no skips, no
failures — then the process is gone. Local collection of the same suite is
2775 items, and 72/2775 = 2.6 % / 144/2775 = 5.2 % reproduce CI's printed
`[ 2%]` / `[ 5%]` marks, so CI's ordering matches. Item **184** is:

```
tests/test_baseline_allocators.py::TestHardOnlyQPAllocator::test_over_cap_holding_infeasible
```

Item 183 (`test_basic_feasible_solve`) passes: a feasible problem is answered
by the primary solver. Item 184 is the first **infeasible** problem in the
suite, and `_solve_cvx` (`src/renquant_pipeline/kernel/portfolio_qp/qp_solver.py:71`)
only reaches its SCS fallback when every earlier solver has failed to return
`optimal`. patsy (statsmodels) and wrapt are not on that path.
[DERIVED — collection order + progress-mark arithmetic; the CI run of this PR
is the confirming experiment]

## Not reproducible off linux/py3.10 — measured, not assumed

| environment | cvxpy | scs | result |
|---|---|---|---|
| macOS arm64, py3.11 (repo `.venv`) | 1.9.2 | 3.3.0 | `36 passed` |
| macOS arm64, py3.11 (throwaway venv, CI versions: numpy 2.2.6, scipy 1.15.3, clarabel 0.11.1, osqp 1.1.3) | 1.7.5 | 3.3.0 | `36 passed` |
| ubuntu-latest, py3.10 (CI) | 1.7.5 | 3.3.0 | interpreter dies |

So the trigger is the linux/py3.10 wheel, not the version pair alone. That is
also why the pin — not a code change — is the right fix here: there is no
defect in our solver chain to correct, and no reproduction to test against.
[VERIFIED]

## Blast radius

- `renquant-pipeline`: every branch. Three PRs blocked.
- `renquant-orchestrator` main CI: green at 18:21Z. `renquant-backtesting`
  main CI: green at 17:45Z. Neither suite exercises an infeasible QP.
  [VERIFIED]
- The **live** umbrella venv is `python 3.10.20 / Darwin arm64 / cvxpy 1.7.5 /
  scs 3.2.11` — the CI version pair minus the bump, on the platform where the
  crash does not reproduce. Live is unaffected today, and this PR changes
  nothing there. It is worth knowing that an unpinned `pip install -U` in that
  venv would pull scs 3.3.0. [VERIFIED — read-only import of
  `RenQuant/.venv/bin/python`]

## Verification

- Green CI on this PR **is** the experiment: the diff touches only the install
  step, so a green run isolates `scs 3.3.0` as the single cause among the three
  package deltas.
- Local suite is unchanged by this PR (it does not read the workflow).

## Removing the pin

Re-test with:

```
uv venv --python 3.10 t && uv pip install --python t/bin/python \
  cvxpy scs numpy scipy clarabel osqp pytest
t/bin/python -m pytest -q tests/test_baseline_allocators.py
```

on a **linux** runner. Drop the pin once CI's resolved cvxpy supports
scs >= 3.3.0 on python 3.10 (today it resolves cvxpy 1.7.5). The pin is
deliberately narrow: no other package in that install line is constrained.
