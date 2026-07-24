# 2026-07-24 — MonitorStateV2 rejects funnel_integrity_history (P-STATE-FILE SOFT fail since 07-11) (PR #212)

STATUS:    delivered
WHAT:      `MonitorStateV2` (`live_state_v2.py`) now declares `funnel_integrity_history`
           as a typed passthrough field (matching the existing `stop_orders` /
           `recent_sell_orders` convention); `to_wire()` omits it when empty so
           the wire shape stays byte-identical to v1 for state files that never
           carried history.
WHY/DIR:   Unblocks the P-STATE-FILE preflight strict flip — under the current
           warn-window it has SOFT-failed the schema check against the live
           book on every run since 2026-07-11; a landing gap (a new task field
           the schema never learned about) would fail the daily run closed
           under strict enforcement.
EVIDENCE:
  artifact:      backtesting/renquant_104/live_state.alpaca.json (RenQuant umbrella, read-only)
  prod or exp:   prod state file, read-only verification only — no write
  existing data: pinned runtime `.subrepo_runtime/repos/renquant-pipeline` (what live
                 runs today) reproduces the logged P-STATE-FILE SOFT-fail verbatim via
                 `LiveStateV2.parse(raw)`, the exact call `preflight_pipeline/tasks/state.py` makes
  best-known?:   only fix on this path; this branch parses the same file to 4 holdings,
                 0 quarantined keys, 7 funnel-history records, round-trip stable
  scope:         "LiveStateV2.parse() on the real live_state.alpaca.json, prod read-only,
                 vs pinned runtime: FAIL before this branch, PASS after"
NEXT:      Merged != deployed — the orchestrator pin must advance before live picks up
           the fix; the generic `monitor_state` contract-test follow-up (any task can add
           an unschema'd key) is worth filing separately per the note below.

## Bottom line

`P-STATE-FILE` has reported a SOFT schema failure against the **live book**
every run since **2026-07-11**. One-line fix: declare the key its own writer
persists. Verified against the real `live_state.alpaca.json` through the exact
call the preflight makes — FAIL before, PASS after.

**This is a staged fail-close.** The preflight text says so itself:
*"warn window — investigate before the strict flip"*. Under the strict flip
this fails the daily run closed on a book that is otherwise healthy.

## The bug

`FunnelIntegrityTask._update_history` (`task_funnel_integrity.py`, landed
**2026-07-11**, `HISTORY_STATE_KEY = "funnel_integrity_history"`) persists a
rolling per-trading-day verdict list onto `ctx.monitor_state` — the same
adapter-persisted vehicle `MonitorIdleStreakTask` uses, capped at
`funnel_integrity.history_window` (default 60).

`MonitorStateV2` (`live_state_v2.py`, last touched **2026-07-04**) declares
`model_config = ConfigDict(extra="forbid")` and seven fields. It never learned
about the eighth.

A new task added a key to a persisted structure without extending the schema
that validates it. Nested models are **not** covered by `LiveStateV2.parse`'s
top-level `extra_quarantine` escape hatch, so an undeclared nested key fails
the entire parse rather than degrading gracefully.

Observed on the live book (identical error 07-23 and 07-24, verdict advancing
daily as the history accrues):

```
preflight ✗ P-STATE-FILE [SOFT] live_state.alpaca.json parses as JSON but
FAILS LiveStateV2 schema (warn window — investigate before the strict flip):
1 validation error for MonitorStateV2
funnel_integrity_history
  Extra inputs are not permitted [type=extra_forbidden,
  input_value=[{'date': '2026-07-16', ...t': 'STRUCTURAL_BLOCK'}], ...]
```

## The fix

1. Declare `funnel_integrity_history: list[dict[str, Any]] = []` on
   `MonitorStateV2` as a **typed passthrough** — the record shape is owned by
   `FunnelIntegrityTask`, so this matches the existing `stop_orders` /
   `recent_sell_orders` convention in the same model rather than inventing a
   sub-model that would become the next fail-close when the record evolves.
2. `to_wire()` omits the key when empty, so a state file that has never
   carried history serializes **byte-identically to the v1 shape**. Round-trip
   is unaffected (parse defaults to `[]`, so dropping an empty list cannot
   lose information) and a rollback to code without the field sees exactly the
   old wire.

Deliberately NOT done: relaxing `extra="forbid"`. That would trade one real
bug for the loss of the guarantee on every field this model does own.

## Verification — against the real live state file, read-only

`LiveStateV2.parse(raw)` — the exact call `preflight_pipeline/tasks/state.py`
makes — on `backtesting/renquant_104/live_state.alpaca.json`:

| module under test | result |
|---|---|
| `.subrepo_runtime/.../renquant-pipeline` (**what live runs today**) | **FAIL** — reproduces the logged error verbatim |
| this branch | **PASS** — 4 holdings, 0 quarantined, 7 history entries retained |

Wire shape after the fix carries the 8 real keys; round-trip identity holds.

## Tests

Four regression tests in `TestFunnelIntegrityHistoryRegression`, built on the
**actual 2026-07-24 live record**:

- `test_history_key_parses` — the live monitor_state shape parses; asserts
  explicitly that nested keys are NOT quarantined, so this class of miss fails
  the whole parse rather than degrading
- `test_history_defaults_empty_when_absent` — pre-07-11 state files unaffected
- `test_history_survives_round_trip`
- `test_writer_key_matches_schema_field` — **binds `HISTORY_STATE_KEY` to
  `MonitorStateV2.model_fields`**, so renaming either side fails here instead
  of silently in preflight against the live book

Full suite: **1977 passed, 9 skipped**. `test_wire_is_v1_flat` (the rollback
contract) initially failed on the always-present empty key and drove fix (2);
it passes unchanged now.

## Deployment

**Merged ≠ deployed.** Live loads `.subrepo_runtime/repos/renquant-pipeline`,
so P-STATE-FILE keeps reporting SOFT-fail until the orchestrator pin advances.
No config, artifact, or state file was touched by this change.

## Follow-up worth filing separately

The generic hole: any task may persist into `monitor_state` without the schema
knowing. `test_writer_key_matches_schema_field` pins one writer. A registry —
or a `monitor_state` contract test enumerating every task that writes to it —
would cover the class instead of this instance.
