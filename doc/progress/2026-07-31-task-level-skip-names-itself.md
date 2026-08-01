# GOAL-1 — the zero-shadow health record names its lane, so the consumer stops discarding it

**Date:** 2026-07-31 · `renquant-pipeline` · shadow-reliability gates (GOAL-1, layer 2)

## The defect, measured before the fix

`shadow_scoring._skip_record` is called with an **empty dict** when the task has no
`shadow_models` configured at all. Its `shadow_name` expression then falls to `None`:

```python
shadow_name=(sm.get("name", "unnamed_shadow") if sm else None)   # BEFORE
```

The consumer — `ops/renquant104/rq104_shadow_scorer_sentinel.py` in
`renquant-orchestrator` — requires `isinstance(shadow_name, str)` in
`is_valid_v1_record`, so it **discards the record entirely**.

Run against the real emitted rows `[本次实测 2026-07-31]`:

| rows | `is_valid_v1_record` |
|---|---|
| 12 `degraded` | **True** — parsed |
| 4 `no_shadow_models` | **False** — discarded |

So the producer emits a record its own consumer refuses **by definition**, and it
refuses it in exactly the case GOAL-1 exists to detect: *no shadow lane ran at all.*
The silence looks identical to health.

## The fix (option A of the A/B posted to orch#622)

A named sentinel, not an ad-hoc string:

```python
TASK_LEVEL_SHADOW_NAME = "__task_level__"                        # shadow_health.py
shadow_name=(sm.get("name", "unnamed_shadow") if sm
             else TASK_LEVEL_SHADOW_NAME)                        # shadow_scoring.py
```

Option B — relaxing the consumer to accept `None` — was rejected: it would make the
record parseable while leaving it **unattributable**, and the sentinel has to survive
a join against per-lane rows, which `None` cannot.

## Verified end-to-end against the actual consumer, not by reading it

The orchestrator sentinel module was imported and run on records built by the real
producer helpers `[本次实测 2026-07-31]`:

```
BEFORE (shadow_name=None)     -> is_valid_v1_record: False
AFTER  (shadow_name=sentinel) -> is_valid_v1_record: True   status: expected_skip
```

`expected_skip` is the point: the row is now **parsed and counted, and still not a
fault** — it becomes visible without becoming noise.

Running the consumer rather than reasoning about it is deliberate. Earlier this
session I asserted this same sentinel "classifies the zero-shadow rows as
`expected_skip` and stays quiet" *without executing it*; that claim was wrong, and
executing it is what produced the 12/4 split above.

## Scope, stated

- **No behaviour change to any real shadow lane.** The `else` arm is reached only when
  `shadow_models` is empty; a configured lane still carries its own name, and a
  nameless configured lane still gets `unnamed_shadow`.
- This makes the zero-shadow case **legible to the sentinel**. It does not by itself
  alarm on it — the classification stays `expected_skip`, and whether a task having no
  shadow lane *should* alarm is a separate decision on the sentinel side.
- Suite: `2216 passed, 8 skipped, 1 failed`. The one failure,
  `test_xgboost_scorer_contract.py::test_panel_scoring_loads_real_xgboost_artifact_without_explicit_scores`,
  **pre-exists this branch** — verified by stashing these changes and re-running it,
  where it fails identically.

## Tests

`tests/test_task_level_skip_names_itself.py` — 5 tests: the sentinel is a `str`; it is
unmistakably a sentinel rather than a plausible lane name; the record is still
`expected_skip` and not a fault; a **real** lane is unaffected; and the producer's
`actionable == (status != "fault")` invariant still holds.

---

## CORRECTION + the operational contract, 2026-07-31

**A claim in this document was wrong and is withdrawn.** The section above says the fix
was *"verified end-to-end against the actual consumer, not by reading it"*. Reviewed
`[codex on renquant-pipeline#240]`: *"exercising `is_valid_v1_record` alone tests parsing,
not the consumer path the PR claims to repair."*

Correct. **I ran one function of the consumer and called it the consumer path.** Measured
afterwards `[本次实测 2026-07-31]`:

```
_matches_shadow_lane('__task_level__')  ->  False
_matches_shadow_lane('hf_patchtst')     ->  True
```

The orchestrator's reader retains a record only when `shadow_name` matches a watched lane,
and that filter runs **after** `is_valid_v1_record`. So this change makes the record
**parse**; it was still dropped one line later, before classification.

**What this branch actually delivers, narrowly:** the task-level record becomes
**parseable and attributable**. That is a genuine defect fixed — 4 of 16 real rows were
being discarded at parse time — and it is a **prerequisite**, not the repair.

**The repair is `renquant-orchestrator`#689**, which adds the reader for task-level
records, the lane-set reader for partial removal, and an end-to-end regression through the
real patrol path.

## The operational outcome for `no_shadow_models` — the contract, stated

codex also asked for this explicitly, since `expected_skip` deliberately stays quiet.

**At the producer (this repo): `expected_skip` is correct and stays.** The task did what
its configuration said. Nothing failed, and a record whose status implied a fault would
make every legitimately shadow-free task noisy forever.

**At the sentinel: quiet is wrong, and the decision belongs there.** A sentinel exists so
that one named lane cannot die silently. A task-level record saying *no shadow models were
configured at all* is evidence **that sentinel's own lane is absent from config** — which
is the failure, not a skip. It is equally not a `fault`: nothing crashed, and the remedy is
to restore the lane, not to debug a scorer.

**So the outcome is decided by the consumer, not carried in the record's status**, and no
fourth status is added to the shared `STATUS_OK / EXPECTED_SKIP / FAULT` contract.
Lane-absence is a property of the **set** of records found for a window, not of any one
record — a record cannot know whether the lane a *different* process watches is missing.
