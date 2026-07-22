# Progress — shadow-scorer health record (canonical silent-failure contract)

Date: 2026-07-21
Deliverable: `src/renquant_pipeline/kernel/panel_pipeline/shadow_health.py` (NEW,
canonical contract) + `.../shadow_scoring.py` (emitter) +
`tests/test_shadow_scorer_health_record.py`. Pipeline-only; no orchestrator /
umbrella / other-repo edits.

## STATUS

DONE — implemented, unit-tested (33 tests in the new file), full suite green
except 2 PRE-EXISTING platform-drift failures in `test_replay_d6_conventions.py`
(`t_stat` null-vs-computed byte-identical pin; fails identically on clean
origin/main with my changes stashed — unrelated to this work). Addresses codex
CHANGES_REQUESTED on #211 (artifact identity, expected-skip state, per-early-exit
tests).

## WHAT

`ApplyShadowScoringTask` emits ONE structured, machine-readable HEALTH RECORD
per configured shadow model per run to an append-only JSONL sink, so a
downstream orchestrator sentinel can catch silent degradation of the shadow
(PatchTST) panel scorer. The shadow stays 100% NON-FATAL. All the pure
resolve+identity+verdict logic lives in the new stdlib-only `shadow_health`
module — the CANONICAL contract the CI gate (#525) and the sentinel (#566)
import so three consumers never drift.

- **Sink (documented for the sentinel):**
  `<config["_strategy_dir"]>/logs/shadow_scorer_health.jsonl` — append-only,
  one JSON object per line, `schema: "shadow_scorer_health.v1"`. Overridable via
  `config["shadow_health"]["path"]`; skipped when no strategy_dir/override (no
  bare-cwd scatter). `config["shadow_health"]["enabled"]=false` is a health-only
  kill switch (never disables shadow scoring).

- **Artifact IDENTITY, not path-existence** (codex point 1): `content_sha256`
  is the IMMUTABLE `sha256:<16hex>` of the file scoring actually loaded (hashed
  via the canonical `content_digest`, `(path,mtime,size)`-cached so sims don't
  re-hash) — a swapped file changes the digest, so a stale-identity "healthy"
  record is impossible. `config_fingerprint` is the training-config identity.
  Required identity absent (`missing_content_sha256` / `missing_config_fingerprint`)
  OR a mismatch against a config pin (`expected_content_sha256` /
  `expected_config_fingerprint` → `content_sha256_mismatch` /
  `config_fingerprint_mismatch`) ⇒ FAULT. Canonical resolver+identity entry
  point: `shadow_health.resolve_artifact_identity(ref, *, strategy_dir,
  repo_root=None) -> ArtifactIdentity` — delegates path resolution to the
  established ONE authority `kernel.artifact_resolver` (absolute → strategy_dir
  → repo_root), so this task / #525 / #566 resolve the same ref to the same
  file.

- **Expected-skip vs fault** (codex point 2): a record is emitted BEFORE every
  early return. `status ∈ {ok, expected_skip, fault}` and `actionable ==
  (status != "fault")` are the sentinel decision axis. A by-design non-run
  (`disabled` / `no_shadow_models` / `no_candidates`) is `loaded=false` yet
  `actionable=TRUE` (`status=expected_skip`) — NOT a fault, NOT silence. A real
  setup/degradation problem (`unresolved_artifact` / `load_failed` / `degraded`
  / `not_scored`) is `actionable=FALSE` with `reasons` tokens. `state` gives the
  precise sub-state. MLflow-setup failure no longer early-returns (it disables
  only MLflow logging; health is still assessed).

- **Record fields:** `schema`, `run_date`, `run_id`, `shadow_name`, `kind`,
  `artifact_path`, `artifact_resolved`, `artifact_resolved_path`,
  `artifact_source`, `content_sha256`, `config_fingerprint`,
  `expected_content_sha256`, `expected_config_fingerprint`, `loaded`,
  `load_error`, `effective_train_cutoff_date`, `staleness_days`, `n_candidates`,
  `n_scored`, `coverage_frac`, `skip_reason`, `state`, `status`, `actionable`,
  `reasons`.

## WHY-DIR

The shadow scorer is fail-soft BY DESIGN. Failure mode: a broken `../../`
artifact_path made it load-fail and `continue`, so the shadow produced NOTHING
for a long time — a G4-critical comparison feed died and nothing alarmed. Path
existence alone is insufficient (a mutable path can be swapped), so the record
captures immutable content identity; and mapping every non-load to "fault"
would blind the sentinel to intentional skips, so expected-skip is a first-class
`actionable=true` state. The shared logic is a dedicated pure module (not buried
in the scoring task that drags pandas/torch) precisely so the CI gate and
sentinel import the SAME resolver+verdict and cannot drift — the exact class of
the 2026 shadow-dead-for-a-week incident (#114).

## EVIDENCE

- `make test` → `2 failed, 1963 passed, 10 skipped`; the 2 failures are the
  pre-existing `test_replay_d6_conventions` platform-drift pins (verified: they
  fail identically with this PR's changes stashed).
- `tests/test_shadow_scorer_health_record.py` (33 tests): `content_digest`
  (missing / swap-detection / dir), `resolve_artifact_identity`
  (strategy_dir / repo_root / unresolved / no-strategy-dir sources), the pure
  `finalize_shadow_health` verdicts (ok / stale / low-cov / missing identity /
  pinned mismatch / pinned match / future / unresolved-vs-load-failed /
  not-scored), expected-skip semantics, and task-level integration for EACH
  early exit (disabled / no_shadow_models / no_candidates / no_primary_scores),
  identity-mismatch, health kill switch, one-record-per-model.
- Regression: `test_shadow_scoring_degenerate_matrix_guard.py` +
  `test_shadow_artifact_resolution.py` green; no cwd pollution; ruff clean on the
  new module + tests (only the 2 pre-existing `shadow_scoring` warnings remain).

## NEXT (consumers of this contract)

- **CI gate #525** — import `shadow_health.resolve_artifact_identity`; fail the
  build when a configured shadow `artifact_path` is `resolved=false` or required
  identity is absent, using the SAME resolver as runtime.
- **Sentinel #566** — tail `<strategy_dir>/logs/shadow_scorer_health.jsonl`;
  alarm iff, for a configured shadow, the latest record is `status=="fault"`
  (== `actionable==false`) — or NO record — for ≥ N consecutive runs.
  `expected_skip`/`ok` never alarm; `reasons` classify the fault; a changed
  `content_sha256` without a config change is optional drift advisory. Gate the
  parse on `schema == "shadow_scorer_health.v1"`.
