# Progress — shadow-scorer health record (silent-failure sentinel feed)

Date: 2026-07-21
Deliverable: `src/renquant_pipeline/kernel/panel_pipeline/shadow_scoring.py`
(+ `tests/test_shadow_scorer_health_record.py`). Pipeline-only; no
orchestrator / umbrella / other-repo edits.

## STATUS

DONE — implemented, unit-tested (15 new tests green), full suite green except
2 PRE-EXISTING platform-drift failures in `test_replay_d6_conventions.py`
(`t_stat` null-vs-computed byte-identical pin; fails identically on clean
origin/main with my changes stashed — unrelated to this work).

## WHAT

`ApplyShadowScoringTask` now emits ONE structured, machine-readable HEALTH
RECORD per configured shadow model per run to an append-only JSONL sink, so a
downstream orchestrator sentinel can catch silent degradation of the shadow
(PatchTST) panel scorer. The shadow stays 100% NON-FATAL — every existing
soft-fail branch and `log.warning` is preserved; the record is emitted from a
per-model `try/finally` so a `continue` on any failure path still writes
exactly one record.

- **Sink (documented for the sentinel):**
  `<config["_strategy_dir"]>/logs/shadow_scorer_health.jsonl` — append-only,
  one JSON object per line, `schema: "shadow_scorer_health.v1"`. Overridable
  via `config["shadow_health"]["path"]`. When no `_strategy_dir` and no
  override is configured (unit/sim edge) the write is SKIPPED rather than
  scattering the file in a bare cwd. Pattern mirrors the existing
  `AdmissionShadowLoggerTask` → `logs/admission_shadow.jsonl` sink.
- **Record fields:** `schema`, `run_date`, `run_id`, `shadow_name`, `kind`,
  `artifact_path`, `artifact_resolved` (bool), `artifact_resolved_path`,
  `loaded` (bool), `load_error` (str|null), `effective_train_cutoff_date`,
  `staleness_days`, `config_fingerprint`, `n_candidates`, `n_scored`,
  `coverage_frac`, `skip_reason`, `actionable` (bool), `reasons` (list).
- **Load-time artifact-resolution check (the `../../` class):** the configured
  `artifact_path` is resolved and `Path.exists()` recorded as
  `artifact_resolved`; when it does not resolve, `load_error` NAMES the
  offending path and the verdict is `reasons == ["artifact_unresolved"]`.
- **`actionable` verdict** (`finalize_shadow_health`, a pure/testable
  function): a shadow is actionable only if it loaded, has a fresh-enough
  `effective_train_cutoff_date` (default ≤ 28d, `shadow_health.max_staleness_days`),
  carries a `config_fingerprint`, and scored a high-enough fraction of the
  candidate cross-section (default ≥ 0.80, `shadow_health.min_coverage_frac`).
  Every failing dimension appends a stable reason token
  (`artifact_unresolved` | `load_failed` | `missing_train_cutoff` |
  `unparseable_train_cutoff` | `stale_<n>d_limit_<m>d` |
  `train_cutoff_future_<n>d` | `missing_config_fingerprint` |
  `low_coverage_<c>_min_<m>` | `<skip_reason>` | `no_scores`).

## WHY-DIR

The shadow scorer is fail-soft BY DESIGN (a shadow-scorer problem must never
fail-close the live decision pipeline). The failure mode: a broken `../../`
artifact_path made it load-fail and `continue`, so the shadow produced NOTHING
for a long time — a G4-critical comparison feed died and nothing alarmed,
because "shadow failed" is non-fatal and the only signal was a per-run log
line. Fix = make the failure VISIBLE without making it fatal: a persisted,
queryable per-run health record is the smallest change that lets a monitor
alarm on silent degradation. Chose the existing JSONL-sidecar-under-`logs`
pattern (not a new runs-DB table) because it is the sink the pipeline task can
write DIRECTLY (the runs-DB `record_*` helpers are wired from the umbrella
adapters, which this repo does not own) and it exactly matches
`AdmissionShadowLoggerTask`, another observe-only shadow feed.

## EVIDENCE

- `make test` → `2 failed, 1948 passed, 10 skipped`; the 2 failures are the
  pre-existing `test_replay_d6_conventions` platform-drift pins (verified: they
  fail identically with this PR's changes stashed).
- New: `tests/test_shadow_scorer_health_record.py` (15 tests) — loaded+
  actionable, unresolved-artifact (`../../`), load-failed, stale cutoff, low
  coverage, missing/future/unparseable provenance, one-record-per-model, sink
  path defaulting + override, and the pure `finalize_shadow_health` verdicts.
- Regression: existing `test_shadow_scoring_degenerate_matrix_guard.py` and
  `test_shadow_artifact_resolution.py` still green; verified no cwd pollution.

## NEXT

- Orchestrator side (NOT this repo): a sentinel that tails
  `<strategy_dir>/logs/shadow_scorer_health.jsonl` and alarms when the latest
  run has `actionable == false` for ≥ N consecutive sessions (or no record at
  all — the "feed died" case). Schema tag `shadow_scorer_health.v1` gates the
  parse.
- Optional follow-up: surface `actionable` / `reasons` into the shadow ntfy
  summary alongside the existing `_shadow_summary` stash.
