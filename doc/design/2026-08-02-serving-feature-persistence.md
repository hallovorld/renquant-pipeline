# Serving feature persistence — the daily run stops discarding what it computed

**Status:** PROPOSAL for review (design only; no code in this PR).
**Unblocks, by construction:** orchestrator#647 / task "serving feature vectors
are never persisted" (the rq105 Stage-3 T-1 feature-snapshot producer cannot
exist while its input is discarded) AND orchestrator#703 (GOAL-4: score
attribution is impossible after the panel refreshes, because the served
feature matrix cannot be reconstructed).

## The measured gap `[VERIFIED — orch#678's measurement, 2026-07-31; shape unchanged on today's main]`

The daily run computes the full serving feature matrix inside the panel
scoring path (`PanelScorer` / `transform_feature_frame`), uses it, and drops
it: the run bundle carries **0** feature keys while its `decision_trace`
carries the CONCLUSIONS (`panel_score`, `expected_return`, `confidence`) for
~290 names. Reconstruction later is impossible in principle: the live panel
refreshes daily and (measured 2026-08-02 on the Job B golden) input vintages
are NOT byte-reproducible after a rebuild — what was served on day T cannot be
re-derived on day T+k.

## Proposal

At the point the serving matrix exists (immediately after the serving
transform, before scoring), persist ONE artifact per run:

```
<run_output_dir>/serving_features.parquet     (~290 rows × ~172 cols ≈ sub-MB)
```

- Columns: `ticker` + the exact feature columns AS SERVED (post-transform,
  the matrix the scorer consumed — not raw panel rows).
- Sidecar keys in the run bundle (additive, absent-tolerant, the
  serving_bundle/g4_session/wf_gate_provenance idiom):
  `serving_features = {path, sha256, n_rows, n_cols, feature_cutoff,
  feature_builder_version, panel_read_sha256}`. Named `feature_builder_version`
  (not a new `transform_version` term) to match the existing downstream
  contract verbatim — `FeatureSnapshot.from_mapping`
  (`renquant_orchestrator/realtime_data_plane.py:150-152`) and `RunProvenance`
  (`renquant_orchestrator/shadow_realtime_serving.py:92-110`) both require
  this exact key. Reusing it here is what makes the Stage-3 producer a real
  formatting step instead of needing a translation layer.
- The writer NEVER raises into the decision path (record-don't-raise; a
  failed write records `status: write_failed` in the sidecar block).
- Retention: files live under the run output dir like the rest of the bundle;
  no rotation policy here (bundle retention is the orchestrator's existing
  concern).

## Contracts this satisfies

1. **Stage-3 producer** (orch#647): the T-1 `FeatureSnapshot` becomes a
   formatting step over yesterday's `serving_features.parquet` — the three
   contract keys (`feature_cutoff`, `feature_builder_version`, `features`)
   map 1:1 onto the sidecar + parquet.
2. **Attribution** (orch#703): "why did name X score s on day T" becomes
   answerable forever: the exact served vector is on disk with a digest, and
   the digest is in the run bundle (the AC6 R4-validated surface).
3. **Evidence-vs-live parity** (pipeline#248's divergence): with served
   matrices persisted, the recipe-vs-serving transform divergence becomes
   measurable PER RUN instead of via one-off forensics — the first standing
   dataset for closing that gap.

## What this is NOT

- Not a schema change to decision_trace or any consumer-visible surface —
  purely additive (one new file + one additive bundle block).
- Not a PIT panel snapshot: it persists the ~sub-MB SERVED matrix only, not
  the ~800MB panel. `panel_read_sha256` records which panel bytes produced it.
- Not enabled-by-flag: persistence is unconditional once merged (a record of
  what happened, same class as decision_trace itself). The only failure mode
  is a recorded write failure, never a changed decision.

## AC6 gate-design rule

N/A — no capital-admission gate is added, tightened, or loosened; this is a
recorder on an existing computation.

## Rollout

1. This design review.
2. Implementation PR in this repo (writer + sidecar block + tests: written
   matrix equals the matrix the scorer consumed BYTE-FOR-BYTE on a synthetic
   session; write-failure records and does not raise; bundle block shape).
3. Orchestrator consumes the sidecar into the run bundle (its existing
   additive-block pattern) — separate small PR.
4. Pin batch (operator). After the first live run persists a matrix, the
   Stage-3 producer design (orch#647) can finally be written against real
   bytes.
