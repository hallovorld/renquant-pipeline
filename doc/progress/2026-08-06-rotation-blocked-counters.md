# 2026-08-06 — A blocked rotation is now countable, not just log-visible

STATUS:   READY FOR REVIEW. 13 new tests; full suite 2481 passed / 8 skipped /
          0 failed. Observability only — no admission, sizing, or order logic
          changes, and no rotation outcome changes.

WHAT:     `ValidatePairsTask`'s three guard rejections (wash_sale, sector_cap,
          correlation_guard) now record onto `ctx.rotations_blocked` via a new
          `record_rotation_block()` helper, and the considered count is preserved
          as `ctx.rotations_considered` before `ctx.rotations` is overwritten with
          the survivors. The run-summary line in `pp_inference.py` reads the
          preserved count, falling back to the old expression for contexts that
          never reached the task.

WHY/DIR:  Measured on the production lane 2026-08-06 — **both** of the day's runs
          logged a rejection and **both** reported zero:

```
kernel.pipeline.rotation: ROTATION_REJECT  swap=NVDA→CRWD  reason=correlation_guard
kernel.pipeline:          InferencePipeline DONE  rotations_emitted=0 (considered=0  blocked=0)
```
`[VERIFIED — logs/daily_104/2026-08-06.log:501,503 (04:43 run) and :1051,1053 (05:12 run)]`

          Across every production daily-full log on disk — 79 files, 23 of which
          ran the rotation tree — the tree selected **4** swaps, **all 4** were
          rejected by `correlation_guard`, **0** executed, and this counter read
          zero on every one `[VERIFIED — parsed ROTATION_TREE / "→ swap" /
          ROTATION_REJECT counts, production lane only (pure-date filenames)]`.

          Two independent causes, both fixed:

          1. Every rejection was `log.info("ROTATION_REJECT ...")` + `continue`,
             recording nothing on `ctx`. `rotations_blocked` was only ever
             written by `EmitRotationsTask`, and only for the
             bear_only/skip_buys/buy_blocked suppression case — never for a
             guard.
          2. `ctx.rotations = validated` overwrites the considered list with the
             survivors, so the summary's `len(ctx.rotations)` was the **survivor**
             count printed under the label "considered". The 2026-04-25 ROT-COUNTER
             fix corrected this line in the other direction (emitted vs
             considered) and left this half wrong.

          The consequence is the failure mode that matters: the single line a
          health check, daily digest, or drift scan would read says *"rotation had
          nothing to do"* on precisely the runs where it tried something and was
          stopped. The truth existed only as INFO-level prose.

EVIDENCE:
artifact:      `kernel/pipeline/task_rotation.py`, `kernel/pipeline/pp_inference.py`,
               `tests/test_rotation_blocked_counters.py`
prod or exp:   **prod code path**, observability only. No guard threshold, no
               admission decision, no order changes — a run that rotated before
               rotates identically now.
existing data: `logs/daily_104/2026-08-06.log` and the 79-log production corpus
               cited above.
best-known?:   yes for the counter defect. **No** for why rotation never
               executes — that is renquant-pipeline#272 and is untouched here.
scope:         `ValidatePairsTask` + the one summary line.

Shape check: the record written matches the one `EmitRotationsTask` already
writes (`{"sell", "buy", "reason"}`), so `kernel/persistence.py:2023`, which
already reads `ctx.rotations_blocked`, needs no change — verified by a test
rather than by inspection, because a differing key set would be dropped silently
rather than raising.

## NOT ESTABLISHED

1. **That fixing the counters makes rotation execute.** It does not. Rotation is
   still 0-for-4 against `correlation_guard`; this change only makes that
   condition visible to something other than a human reading logs. The
   structural question — a 3-candidate pool where the only threshold-clearing
   name is redundant with a holding — is renquant-pipeline#272.
2. **That any monitor currently consumes these counters.** They are logged, and
   `rotations_blocked` reaches the decision ledger; whether a scan reads them is
   an orchestrator-side question not measured here.
3. **Whether the 4 blocked swaps would have been profitable.** Not estimated,
   and not the point.

## NEXT

With `blocked` non-zero, a chronic-block condition becomes detectable from the
run summary alone. Wiring an ops-audit member on it is a separate change and
should wait until at least one real run has produced a non-zero value, so the
detector is written against a measured shape rather than an expected one.

## REVERT

Delete `record_rotation_block` and its three call sites in `task_rotation.py`,
delete the `ctx.rotations_considered` assignment, restore
`n_considered = len(ctx.rotations)` in `pp_inference.py`, and delete
`tests/test_rotation_blocked_counters.py`. No other file changes.
