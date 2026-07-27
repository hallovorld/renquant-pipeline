# WF sim-time provenance persistence contract (design)

**Status:** DESIGN — no implementation in this PR. Root unblock for the G4
evidence chain (codex reviews on model#64/#65/#66: post-hoc reconstruction of
which fold/artifact scored which date is inadmissible; provenance must be
persisted at generation time).

**Refs:** common#33 + pipeline#214 (canonical fold selection, sequenced),
model#64 (admissibility input types), model#65/#66 (Phase-A converter, blocked
on this), orch#572 (closed; verdicts return only via this chain).

## 1. Problem

`score_distribution` records only the scorer FAMILY (`active_scorer` =
`"xgb"`/`"hf_patchtst"` via `decision_trace.active_scorer_identity`), never
the walk-forward fold that actually scored the date. The Phase-A converter
therefore replays `entry_as_of` semantics against the manifest and re-hashes
artifacts long after the run (`select_pit_fold`, `resolve_artifact_digest`) —
exactly the post-hoc reconstruction the review rejects: a score file can be
stamped with a fold/digest the converter inferred later, with no evidence the
sim produced that score under that fold/manifest/code revision.

## 2. Contract

### 2.1 Two-phase append-only records (codex P1 round 1: a resolver row alone
### does not bind to the score Phase-A consumes)

`schema_version: "wf_sim_provenance.v1"`. Append-only JSONL; two record
kinds, both keyed by `(sim_run_id, prediction_date)`. A date's provenance is
COMPLETE only when the pair exists and cross-matches; extraction rejects
orphaned (either kind alone), duplicated (same kind+key twice), or
mismatched pairs as inadmissible.

**`record_kind: "fold_resolved"`** — emitted at the loader boundary when the
fold that will serve scoring is resolved:

| group | fields | source (in scope at emit time) |
|---|---|---|
| identity | `sim_run_id`, `prediction_date`, `seed` | run_backtest args / ctx |
| fold | `cutoff_date`, `trained_date`, `effective_train_cutoff_date`, `lookahead_days`, `artifact_uri`, `calibrator_uri` | the selected `RetrainEntry` (loader `entry_as_of`) |
| manifest | `manifest_path`, `manifest_digest` | `loader._manifest_path` + file bytes |
| artifact | `artifact_digest`, `is_real_content_digest`, `family`, `fingerprint_schema` | `_scorer_claim_for_entry` (already hashes the fold artifact bytes at load) |
| calibrator | `calibrator_digest` (nullable) | `calibrator_as_of` resolved path |
| code | `revision_pins` (umbrella + pipeline + model + backtesting + common + artifacts) | reuse `pit_parity_ledger.commit_path_fingerprint`'s multi-repo pin capture — NOT the cwd-only `_commit_sha` |
| audit | `emitted_at_utc` (audit-write clock ONLY — never a decision-time claim) | wall clock |

**`record_kind: "score_committed"`** — emitted at the commit point AFTER the
per-date score observation is successfully persisted
(`RecordScoreDistributionTask.run`, post-INSERT), binding the provenance to
the exact observation Phase-A will read:

| group | fields | semantics |
|---|---|---|
| join | `sim_run_id`, `prediction_date`, `score_observation_key` = `(run_id, date, run_type)` | the `score_distribution` primary-key coordinates of the persisted rows |
| binding | `score_payload_digest` = `sha256:<64hex>` over the canonical serialization of the persisted score series (rows sorted by ticker; fixed field order `(ticker, raw_panel, mu, rank_score, sigma)`; floats via `repr`), `n_rows` | computed from the exact frame handed to the INSERT — extraction recomputes over what it reads back and requires equality |
| pairing | `artifact_digest` (echo of the `fold_resolved` value) | direct pair-integrity check without a join through the fold table |
| time | `score_timestamp` (REQUIRED — see §2.2), `emitted_at_utc` (audit only) | |
| durability | `persisted: true/false` | `false` when the sim runs with persistence off (`--no-persist` / `dump_walkforward_sim_metrics`); such rows are STILL emitted (from the adapter, post-scoring, digesting the in-memory payload) but are Phase-A-INADMISSIBLE by construction, since Phase-A reads the sim DB |

A retry/re-score of the same bar appends a NEW `score_committed` row;
extraction treats >1 `score_committed` for one key as a duplicate and
rejects the date unless the rows are byte-identical (idempotent re-emit).

### 2.2 Decision-time semantics (codex P1 round 1: audit-write time cannot
### certify a historical decision)

`score_timestamp` = the SIMULATED session's decision timestamp for the bar —
the same instant the pipeline stamps into the run record for that simulated
date (source: the sim ctx's decision timestamp for `prediction_date`;
timezone America/New_York, persisted as ISO-8601 with offset; the
`decision_schedule.run_bundle_timestamp` convention). A rerun performed
today certifies a 2024 decision via THIS field; `emitted_at_utc` is
explicitly audit-only metadata in both record kinds.

PIT invariant, enforced at emit: `input_watermark <= score_timestamp` — the
feature store served to the scorer must not contain events after the
simulated decision instant. A violation still emits (append-only honesty)
with `pit_violation: true`, and extraction rejects the date.

| group | fields | source |
|---|---|---|
| inputs | `input_watermark` (max event time of the feature store actually served), `pit_violation` | ctx data axis, checked against `score_timestamp` |

Digest grammar: `sha256:<64 hex>` and artifact refs `sha256:<64hex>@<locator>`
verbatim from the admissibility ledger (`LABEL_REF_RE`) — the consumer is the
admissibility chain, so we use its grammar end-to-end. (Deliberately NOT the
#211 16-hex abbreviated observer form; that contract serves runtime shadow
health, a different consumer. One producer, one grammar, converters may
abbreviate downstream. The 2026-07-26 strategy#66 incident is the cautionary
tale for mixing these two forms.)

PatchTST divergence is carried honestly: `.pt` checkpoints hash as whole-file
bytes (`loader.py` already drops to file-hash for non-JSON artifacts);
`fingerprint_schema` records which dispatch path stamped the digest
(v1 vs `accept_legacy_stamps`), so extraction never has to guess vintage.

### 2.3 Emit sites: loader boundary (fold_resolved) + persistence commit
### point (score_committed)

A single `provenance_sink` (small protocol object: `emit(record: dict) ->
None`, append-only JSONL writer, fsync per row) shared by both sites:

- `fold_resolved`: handed to `WalkForwardModelLoader` at construction;
  emitted once per `entry_as_of`-resolution served to scoring (dedup per
  key inside the sink; re-entrant calls for the same bar emit once).
  `entry_as_of`/`model_as_of`/`_scorer_claim_for_entry` are the ONLY places
  where fold row + resolved artifact path + digest co-exist, and all sim
  entry points (`run_sim_104` driver, `dump_walkforward_sim_metrics`,
  `weekly_wf_promote`) funnel through this seam.
- `score_committed`: emitted by `RecordScoreDistributionTask.run`
  immediately AFTER its successful INSERT (persistence on), or by the sim
  adapter post-scoring with `persisted: false` (persistence off). The task
  reaches the sink and the fold echo via ctx (the adapter stamps
  `ctx._wf_provenance_sink` / `ctx._wf_active_fold` when it binds the
  fold's scorer).
- The umbrella `sim.runner` adapter change stays construction-time small
  (pass the sink; stamp ctx), keeping the cross-repo diff minimal.

### 2.4 Durability: JSONL beside the run bundle, NOT a sim-reset table

`data/wf_provenance/<sim_run_id>.jsonl`, append-only, never truncated. The
sim DB (`sim_runs.db`) is TRUNCATEd every `run_backtest`
(`clear_sim_tables`/`_SIM_RESET_TABLES`), so a provenance table there would
survive only the last run; exempting a new table is possible but couples
provenance to `persistence.enabled`, which `dump_walkforward_sim_metrics`
turns off. The JSONL path is therefore primary. SECONDARY (cheap, no schema
change): when persistence IS on, mirror `training_cutoff` +
`model_content_sha256` + the record itself into the existing `pipeline_runs`
columns (`training_cutoff`, `model_content_sha256`, `run_bundle_json`) for the
per-bar row — pure column reuse, registered in `_COLUMN_MIGRATIONS` only if a
column is missing in old DBs.

### 2.5 Extraction becomes a read + verify, reconstruction becomes a
### cross-check

`build_phase_a_inputs` (model repo, rebased #65) consumes the JSONL as the
ONLY source of fold/artifact identity, and for each date MUST:

1. require the complete `fold_resolved` + `score_committed` pair with
   matching keys and matching `artifact_digest` echo — orphans, non-identical
   duplicates, `persisted: false`, and `pit_violation: true` rows are
   inadmissible;
2. read the score rows AT `score_observation_key` from `score_distribution`,
   recompute `score_payload_digest` over what it read (same canonical
   serialization), and require equality with the recorded digest plus
   `n_rows` — proving the consumed observation IS the one the sim committed;
3. run `select_pit_fold` + `resolve_artifact_digest` ONLY as independent
   cross-checks: any mismatch between recorded and re-derived identity is a
   HARD error (evidence quarantined), never a fallback.

`is_real_content_digest=False` rows are inadmissible for GO/KILL evidence by
construction.

## 3. Sequencing (tight, same order as reviews demand)

1. common#33 → pipeline#214 (canonical selector; merged first so the emit
   hook sits on ONE fold-selection implementation, not two).
2. THIS design → codex review.
3. Implementation PRs: pipeline (sink protocol + loader emit +
   `pipeline_runs` mirror), umbrella (adapter construction line),
   backtesting (driver plumb-through of seed).
4. model#65 rebase: converter reads the ledger; #66 isolation rebases on top.
5. Reruns: XGB multi-seed (PRE-registered: seeds and disposition rule frozen
   in a prereg doc BEFORE launch; single-split single-seed results remain
   exploratory). PatchTST rerun is compute-gated (no-Modal rule stands);
   its plan lands in the prereg doc, not here.

## 4. Non-goals

No disposition of G4 here (GO or KILL only via the pre-registered rule on
admissible evidence). No change to live daily scoring paths — the sink is
constructed only by sim entry points; daily-full never instantiates it
(zero live-surface delta; the loader default is `provenance_sink=None`,
behavior identical).

## 5. Revert

Design doc only; revert = git revert.
