# WF sim provenance sink — fold_resolved + score_committed emitters (design #215)

**STATUS:** Implemented + tested — the PIPELINE piece of the merged
`doc/design/2026-07-27-wf-sim-provenance-contract.md` (§3 step 3). The
umbrella adapter (sink construction + ctx stamping) and the backtesting
driver seed plumb are follow-ups, per the design's §2.3 seam.

**WHAT:**

- New `src/renquant_pipeline/kernel/walk_forward/provenance.py`:
  - `ProvenanceSink` protocol (`emit(record: dict) -> None`) +
    `JsonlProvenanceSink` — append-only JSONL, fsync per row, targeting
    `<dir>/<sim_run_id>.jsonl` (canonical dir `data/wf_provenance`, design
    §2.4). Never truncates; appends across sink instances of the same
    `sim_run_id`.
  - Record constructors `build_fold_resolved_record` /
    `build_score_committed_record` implementing the §2.1/§2.2 schemas
    exactly: `schema_version "wf_sim_provenance.v1"`; FULL digest grammar
    `sha256:<64hex>` enforced (the admissibility-ledger `LABEL_REF_RE`
    family, never the #211 16-hex observer form); `score_timestamp`
    REQUIRED + timezone-aware on `score_committed`; `emitted_at_utc`
    audit-only (stamped by the sink at write time, excluded from the
    idempotency identity); PIT invariant `input_watermark <=
    score_timestamp` checked at construction — a breach STILL builds the
    record with `pit_violation: true` (append-only honesty).
  - Canonical score-payload serialization (`canonical_score_payload` /
    `score_payload_digest`): rows sorted by ticker; fixed field order
    `(ticker, raw_panel, mu, rank_score, sigma)`; numerics via
    `repr(float(v))` (ints normalize through float), `None` → JSON null;
    one compact JSON array per row, newline-joined, UTF-8. Byte layout
    pinned in tests for the Phase-A recompute.
  - In-sink dedup: `fold_resolved` re-entrant emits for the same
    `(sim_run_id, prediction_date)` are a no-op; identical
    `score_committed` re-emits are a no-op; DIFFERING content for the same
    key appends (extraction's byte-identity/duplicate rule then rejects the
    date — the sink never masks a real double-resolution or re-score).
  - Sink identity completion: records built where run identity is out of
    scope (the loader) arrive with `sim_run_id`/`seed`/`revision_pins`
    None; `emit` completes them from construction args; a conflicting
    non-null `sim_run_id` is refused (cross-run mix-up).
  - `capture_revision_pins(repos)` — best-effort multi-repo `git rev-parse
    HEAD` helper for the adapter follow-up (see design-vs-code conflicts).
- `kernel/walk_forward/loader.py`: optional `provenance_sink=None`
  constructor arg on `WalkForwardModelLoader`. When set, `entry_as_of`
  emits `fold_resolved` at the resolution actually served (all sim entry
  points funnel through this seam; `model_as_of`/`calibrator_as_of` are
  covered by construction). Emitted identity: the selected `RetrainEntry`
  row; manifest path + whole-file digest (cached); artifact whole-file
  digest + `is_real_content_digest` (False + null digest when the artifact
  file is missing — honest, not fatal at resolution time);
  `family` (artifact suffix split, e.g. `json` vs `pt`);
  `fingerprint_schema` from `_scorer_claim_for_entry`'s dispatch route
  (v1 vs legacy); calibrator whole-file digest (nullable). Default `None`
  ⇒ byte-identical behavior — daily-full never constructs a sink; the
  sink-carrying path only ADDS the emit after all existing guards pass.
  Emit failures propagate (a sim that cannot persist its evidence chain
  aborts loudly).
- `kernel/pipeline/task_score_distribution.py`
  (`RecordScoreDistributionTask.run`): after the successful INSERT +
  commit, if `ctx._wf_provenance_sink` is set (design §2.3 ctx attrs), emit
  `score_committed`: `score_observation_key=(run_id, date, run_type)`;
  payload digest computed from the EXACT tuples handed to the INSERT;
  `n_rows`; `artifact_digest` echo from `ctx._wf_active_fold`;
  `score_timestamp` from the ctx decision timestamp (see below);
  `input_watermark` from `ctx._wf_input_watermark` (adapter-stamped ctx
  data axis; fold-record fallback); `persisted: true`. Emit sits AFTER the
  task's try/except so an evidence-chain failure is loud, while a failed
  INSERT emits nothing. Absent sink attr = no-op, default path unchanged.

**ctx decision-timestamp attribute (as tasked to identify):** the real
attribute is `InferenceContext.run_timestamp`
(`src/renquant_pipeline/context.py:29` — "Wall-clock timestamp for
live/session-aware checks. Sim/LEAN may leave this None to preserve
bar-date-only historical semantics."). When present it is used verbatim
(naive values are interpreted in America/New_York, the convention's tz).
Because sim deliberately leaves it None, the documented fallback (the
design-named `decision_schedule.run_bundle_timestamp` convention, §2.2) is
applied: the simulated session's decision instant = the official US-equity
close, 16:00 America/New_York on the bar date, ISO-8601 with offset — the
same `US_EQUITY_CLOSE` schedule the admissibility ledger's
`_decision_ts_from_schedule` uses. The pipeline repo has no session
calendar dependency here, so early closes are NOT resolved in the fallback;
if the adapter has the real per-bar decision instant it should stamp
`ctx.run_timestamp` and it wins.

**Design-vs-code conflicts (recorded, not silently deviated):**

1. §2.1 `revision_pins` names `pit_parity_ledger.commit_path_fingerprint`
   as the multi-repo pin capture to reuse — **no such function exists in
   any repo**. `renquant-model/experiments/ensemble_phase0/pit_parity_ledger.py`
   is a pure-data comparator whose docstring explicitly delegates pin
   capture to the umbrella harness. Implemented instead:
   `provenance.capture_revision_pins` (best-effort `git rev-parse HEAD`
   over a caller-supplied repo map) for the umbrella adapter to call at
   sink construction; never the cwd-only `persistence._commit_sha`.
2. §2.2 sources `input_watermark` from "ctx data axis" — no such axis
   exists on `InferenceContext` today (no `input_watermark`/
   `max_event_time` field anywhere in this repo outside the
   `decision_schedule` validators). Contract chosen:
   `ctx._wf_input_watermark` (adapter-stamped, same `_wf_` convention as
   the two §2.3 attrs), with the `_wf_active_fold` mapping's
   `input_watermark` key as fallback; `None` is recorded as null with
   `pit_violation: false` — explicitly NOT a pass of a check that could
   not run (extraction owns that judgement).
3. §2.3 `ctx._wf_active_fold` shape is unspecified; the task accepts a
   mapping or attribute object and reads `artifact_digest` (and optionally
   `input_watermark`). The natural adapter choice is the `fold_resolved`
   record dict itself.

**pipeline_runs mirror (design §2.4 SECONDARY):** NOT implemented in this
PR — and there is nothing to implement in THIS repo: `record_pipeline_run`
already accepts `training_cutoff` / `model_content_sha256` / `run_bundle`
and `_COLUMN_MIGRATIONS` already registers the columns
(`kernel/persistence.py:682-690, 1249`), but the repo contains **zero call
sites** — every caller lives in the umbrella adapter. The mirror is
therefore purely the adapter's construction-time line (pass the fold's
cutoff + digest + record into the existing kwargs) and lands with the
umbrella follow-up. Deferred per the design's "SECONDARY" marking; no risk
taken here.

**WHY-DIR:** codex reviews on model#64/#65/#66 ruled post-hoc
reconstruction of which fold/artifact scored which date inadmissible for
G4 evidence; the merged #215 contract makes the sim persist provenance at
generation time, two-phase (resolution + committed observation), so
Phase-A extraction becomes read + verify and reconstruction becomes a
cross-check. This PR is §3 step 3's pipeline piece — the emitters and the
durability surface, with a hard zero-live-delta guarantee (sink default
None; ctx attrs absent on the daily path).

**EVIDENCE:**

- Full suite (umbrella venv python, common 0.15.0 on path):
  **2046 passed, 8 skipped** (baseline before change: 2007 passed,
  8 skipped; +39 = the new `tests/test_wf_provenance_sink.py`).
- New tests cover: sink round-trip + file format + identity completion +
  append-only across instances; full-digest-grammar enforcement (16-hex
  abbreviated form REJECTED); `score_timestamp` required + tz-aware;
  PIT-violation emission (breach still emits, equality is not a breach,
  cross-offset comparison); canonical-payload byte layout pinned + digest
  determinism (row-order independence via ticker sort, int/float
  normalization, float-`repr` stability); loader `fold_resolved` emission
  (served-resolution fields, manifest/artifact/calibrator digests,
  missing-artifact honesty, `.pt` family split, re-entrant per-bar dedup);
  default-None = no file + identical selection; task `score_committed`
  emission (digest recompute over DB read-back equals recorded digest —
  the mini Phase-A step-2 loop), session-close fallback timestamp,
  `run_timestamp` precedence, rerun no-op vs changed-scores append,
  absent-attrs default path, failed-INSERT emits nothing.

**NEXT:**

1. Umbrella adapter (design §2.3/§3 step 3): construct
   `JsonlProvenanceSink(sim_run_id, <data_root>/wf_provenance, seed=...,
   revision_pins=capture_revision_pins(...))` in the sim runner; pass to
   `WalkForwardModelLoader`; stamp `ctx._wf_provenance_sink` /
   `ctx._wf_active_fold` (+ `ctx._wf_input_watermark` when the data axis
   exposes it); emit `persisted: false` rows post-scoring when persistence
   is off; wire the `pipeline_runs` SECONDARY mirror through the existing
   `record_pipeline_run` kwargs.
2. Backtesting driver: plumb the seed through to the adapter.
3. model#65 rebase: `build_phase_a_inputs` consumes the JSONL as the only
   fold/artifact identity source (§2.5 read + verify; reconstruction
   demoted to cross-check).
