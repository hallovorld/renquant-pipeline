# Six documented symbols have a kernel twin; nothing related them   (PR pending)

STATUS:    delivered
WHAT:      Adds `tools/twin_pairs.py` + a committed `twin_pairs.json` pinning, for every
           public `*Task` export, which module it resolves to and the sha256 of **both**
           sides of any same-named kernel twin. Editing one side without the other fails
           the check.
WHY/DIR:   GOAL-3. renquant-orchestrator#623 R1 (merged) names the defect in this
           package: `renquant_pipeline.VetoWeakBuysTask`, the **documented** symbol,
           resolves to `panel_scoring.py` and not to the kernel, so a kernel-only fix
           misses it — which is how pipeline#222 happened. #623's own words: *"the
           failure is not that duplicates exist … it is that nothing in the repo tells
           you which copy executes."*
EVIDENCE:  §1.
NEXT:      #623 R7 (twin-ness *inside* one function) is a different shape and is not
           addressed here.

## §1 EVIDENCE

`[VERIFIED — tools/twin_pairs.py --emit against origin/main @ 0d87b32]`:

| | count |
|---|---|
| public `*Task` exports in `__all__` | **9** |
| of those, resolving to a **non-kernel** module | **9** |
| of those, having a **same-named class under `kernel/`** | **6** |
| resolving *into* the kernel | **0** |

The six twinned symbols are `ApplyGlobalCalibrationTask`, `ApplyScoresTask`,
`BuildFeatureMatrixTask`, `LoadScorerTask`, `RegimeModelAdmissionTask` and
`VetoWeakBuysTask` — all public-side in `panel_scoring.py`, all kernel-side in
`kernel/job_panel_scoring.py`.

**So R1 is not a one-off.** Every documented Task symbol resolves outside the kernel, and
two-thirds of them have a kernel counterpart that nothing mechanically relates to them.

### What is pinned, and why the pair rather than the file

Per pair, the sha256 of *each side's* class source. Changing one side changes one digest
and the check fails — naming the case explicitly when the **kernel** moved and the public
copy did not, because that is the defect: a fix applied to the copy a reader finds first,
leaving the documented symbol on the old behaviour. Changing **both** is the correct way
to fix a twin, so it is reported only as a re-pin prompt and explicitly **not** as the
one-sided defect; a test pins that distinction.

The check is deliberately **noisy in the safe direction** — a comment-only edit trips it
too. Cheap to re-pin, and the alternative (normalising source until only "real" changes
count) would be a second implementation of *what counts as a change*, which is the same
class of defect this file exists to catch.

`--emit` prints and never writes; the pin changes only through a reviewed PR.

## §2 Tests

17 new. The load-bearing ones:

- `test_a_kernel_only_change_is_reported_as_R1` — the actual regression, asserting the
  message names #623 R1 rather than a generic drift;
- `test_both_sides_changing_is_reported_once_and_not_as_R1` — the negative case, so the
  correct fix is not flagged as the defect;
- `test_the_measured_shape_is_pinned` — 9 / 6 / 0. Those three numbers **are** the R1
  finding; if any moves, the finding moved and the doc above is stale;
- unpinned export, retired pin, empty pins and both IO error paths.

**A test expectation I got wrong and corrected rather than accommodated:**
`public_task_names` does not filter a leading underscore, and my first test asserted it
should. Anything listed in `__all__` is exported **by definition**, so filtering it would
make the pin describe a smaller surface than callers can import — the same
"checked set is not the real set" gap this file exists to close. The code was right.

## §3 Suite

| tree | result |
|---|---|
| `origin/main` @ 0d87b32, separate worktree | 51 failed, 2098 passed, 8 skipped |
| this branch | 51 failed, **2115** passed, 8 skipped |

`[VERIFIED — python3 -m pytest -q in both worktrees, sibling checkouts on PYTHONPATH]`.
Same 51 pre-existing failures on both trees; the delta is exactly the 17 tests added.
The 51 are an environment property of this checkout, not something this PR touches — and
they are stated rather than filtered out, since a PR that quietly reports only its own
subset is not reporting a regression signal.

## §4 Scope

This addresses #623 **R1 only**, in the repo that owns it. R7 — twin-ness *inside* a
single function, where one branch is unreachable because no caller passes the argument —
is a different shape that a symbol-resolution pin cannot see, and is not claimed here.
No behaviour changes: the tool is read-only and is not imported by any runtime path.
