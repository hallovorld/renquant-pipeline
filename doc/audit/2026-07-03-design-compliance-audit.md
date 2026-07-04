# Design-Compliance Audit — renquant-pipeline + renquant-strategy-104

Date: 2026-07-03
Scope: the 104 decision core (`renquant-pipeline`) + the config contract
(`renquant-strategy-104`), audited against the umbrella operating model
(`RenQuant/doc/arch/subrepo-operating-model.md`, Universal Rules 1-6), both
repos' `CLAUDE.md`, the #210 ownership table (admission enforcement in
`job_universe`; umbrella owns no model-selection logic), and the standing
rules: single-impl-imports-only (fingerprint/hash/calendar), flags
default-OFF with byte-inertness tests, active==golden config lockstep,
fail-isolated observe-only tasks.

Method: fresh read-only clones; every P0/P1 finding was verified against
source at the quoted `file:line` (pipeline @ 778983a, strategy-104 @ head,
common @ head). Docs-only deliverable — no code changes.

Severity rubric:

- **P0** — violates a hard boundary or can silently corrupt/kill a live
  decision (silent fail-open on a decision path), even if latent today.
- **P1** — design-rule violation with concrete drift or failure risk
  (contract bypass, duplicated impls that must agree, fail-open defaults on
  safety gates, observe-only work that can kill the run, forbidden import
  direction).
- **P2** — hygiene: missing declarations/counters, dead code,
  documented-transition debt.

## Summary

| Severity | Count |
|---|---|
| P0 | 1 (latent; prod config currently masks it) |
| P1 | 24 |
| P2 | 33 |

Overall verdict: the decision core is materially healthier than the recurring
production incidents would suggest — the fail-closed panel-scoring path, the
preflight contract, the two-axis staleness admission gate, and strategy-104's
pin-test discipline are genuinely strong (see Appendix). The findings cluster
in five places:

1. **Divergent safe-defaults** (§5) — pipeline hardcodes numeric fallbacks
   that contradict the strategy-104 value for the same key, so losing a
   config key silently re-arms known-bad behavior. Includes the one P0.
2. **Forbidden import direction** (§2) — the in-repo TrainingPipeline and
   the legacy PatchTST scorer import umbrella-only code (`training.*`,
   `scripts/transformer_v4.py`), and the import-boundary tests have a hole
   exactly there.
3. **Hand-copied impls** (§3) — the M6 fingerprint invariant holds on the
   compare path but leaks on the compute path; session calendars are 5-way
   copied with one already-diverged pair; Hurst and
   `compute_parent_intent_id` are verbatim cross-repo copies.
4. **Twin modules** (§6) — two decision-trace/scorer-identity
   implementations and byte-identical `state_paths.py` twins, all live —
   the calibrator triple-impl bug shape, pre-divergence.
5. **Observe-only fail-isolation gaps** (§1) — the shadow scorer can still
   kill a live run through an unwrapped path-resolution step.

### Top 5 (fix-first order)

| Rank | Finding | Sev | Where |
|---|---|---|---|
| 1 | Kelly σ-horizon default 252 **and preflight PASSES on the absent key** — silently re-arms the documented 2026-06-11 ~4.2x variance bug the config comment itself post-mortems | P0 (latent) | `job_panel_scoring.py:3610` + `preflight.py:1471-1476` (5.1) |
| 2 | alpha158 inference features claim "build script and this module import the same low-level functions" but hand-mirror the operators instead — nothing enforces train/serve feature parity on the LIVE XGB primary path | P1 | `panel_pipeline/alpha158_features.py:13-16,41+` (6.3) |
| 3 | Observe-only `ApplyShadowScoringTask` is not fully fail-isolated: umbrella-root resolution runs outside the try, inside the live panel chain — a shadow-only failure kills the live decision run | P1 | `shadow_scoring.py:244,258` wired at `job_panel_scoring.py:3827` (1.5) |
| 4 | Forbidden import direction: `pp_training*.py` imports umbrella-only `training.*` at 9 sites; `patchtst_scorer.py` sys.path-inserts umbrella `scripts/` and imports `transformer_v4`; the boundary tests miss both by construction | P1 | `pp_training.py:407-820`, `patchtst_scorer.py:34-38,81-82`, `test_import_boundaries.py:22-36` (2.1-2.3) |
| 5 | Divergent-default cluster on the sizing/admission path: `max_concentration` 0.35 vs config 0.12, `model_staleness_days` absent ⇒ gate OFF, `qp_horizon_contract` "warn" vs "strict", `qp_tax_lot_method` "fifo" vs "hifo", rotation bar 0.03 vs 0.06, topup floor 0.20 vs 0.55 | P1 x6 | §5.2-5.7 |

Honorable mention: the live twin modules (6.1/6.2) — byte-identical today,
one edit away from the fingerprint-triple-impl failure class.

## 1. Pipeline-primitive compliance (Universal Rule 1)

Contract baseline (`renquant_common/pipeline.py`): `Task.run(ctx) ->
bool|None` (False = stop chain), `Job` = sequential task chain with
`should_skip`, `Pipeline.run -> PipelineResult` with per-step audit records,
`run_parallel` with per-item fault isolation. Note: common has **no enforced
declared-outputs mechanism** — pipeline tasks use a voluntary
"Reads:/Writes:" docstring convention on a shared-blackboard ctx.

Conformance: **~231 of 259 Task/Job/Pipeline classes (~89%)** subclass the
common primitives (149 `Task`, 22 `Job`, 21 `PreflightTask(Task)`, plus thin
adapters). `kernel/pipeline/pipeline.py` is exemplary (re-exports common,
collapsed the old duplicate executor). No bare `except:` anywhere in src.

### P1

| # | file:line | Finding | Rule | One-line fix | Owner |
|---|---|---|---|---|---|
| 1.1 | `kernel/pipeline/pp_training.py:139-182` | `TrainingTask/TrainingJob/TrainingTickerJob` — bespoke ABCs re-implementing common's contract with shipped semantic drift (`TrainingTask.run` drops the `False` short-circuit) | Universal Rule 1 | subclass/adapt `renquant_common.Task/Job` (moot if 2.1 relocates the module) | pipeline |
| 1.2 | `kernel/pipeline/pp_training_full.py:72-101` | `FullTrainingTask/FullTrainingJob` — `FullTrainingJob.run` is a line-for-line copy of common `Job.run` | Rule 1 | delete, import common | pipeline |
| 1.3 | `kernel/pipeline/job_universe.py:60-68,499-511` | `UniverseTask/UniverseJob` — third bespoke framework; adds per-task `should_skip` divergent from common (Job-level) | Rule 1 | rebase on common Task/Job | pipeline |
| 1.4 | `pp_inference.py:299,618`, `pp_execution.py:62`, `pp_training.py:852`, `pp_training_full.py:406`, `pp_research_acceptance.py:182` | all six top-level pipelines are plain classes with ~300-line hand-rolled `run()` sequences; none compose `renquant_common.Pipeline` ⇒ no `PipelineResult`/`PipelineStepRecord` step audit, no pipeline-level `should_skip` | Rule 1 | compose jobs into `common.Pipeline` (or thin adapter emitting StepRecords) | pipeline |
| 1.5 | `kernel/panel_pipeline/shadow_scoring.py:244,258` | observe-only `ApplyShadowScoringTask` not fully fail-isolated: registry import and `_resolve_shadow_artifact_path` (can raise `RuntimeError` via `_data_root.py:71-86`) run OUTSIDE the try; task sits unwrapped in the live panel chain (`job_panel_scoring.py:3827`) ⇒ a shadow-only path failure kills the live decision run | fail-isolated observe-only | wrap the whole per-model loop body (incl. path resolution) in the existing catch-and-continue | pipeline |
| 1.6 | `kernel/panel_pipeline/task_quality_floor.py:451,458,476` | silent `except Exception: return None` in `_gate_a_threshold`; `_gate_a_distribution_floor(threshold=None)` passes everything ⇒ a score_db error silently disables an ENABLED buy-quality floor, indistinguishable from designed warm-up (dormant today: `distribution_floor.enabled=false`) | "Do not silently fallback" (CLAUDE.md) | log.error + counter; fail closed on DB failure vs warm-up | pipeline |
| 1.7 | `kernel/regime.py:512`, `kernel/pipeline/task_regime.py:349` | `except Exception: pass` silently drops SPY MA50/MA200 bearish-trend evidence (`regime.bear_trend_filter.enabled=true` in prod config) — regime resolves without a defensive input, no counter/trace stamp (mitigated: hard_bear/GMM BEAR routes independent) | silent fallback on decision input | warn + `ctx.counters["spy_trend_input_failed"]` | pipeline |

### P2

| # | file:line | Finding | One-line fix |
|---|---|---|---|
| 1.8 | `shadow_scoring.py:239-426` | per-model shadow failures warn+continue with NO counter (contrast the model citizen `task_admission_shadow.py:336` `_bump_counter`); the silent-shadow-darkness class already bit prod for 3 days | `_bump_counter` per swallow |
| 1.9 | `pp_inference.py:324` | challenger observability `except Exception: pass`, no counter | counter |
| 1.10 | `kernel/pipeline/task_score_distribution.py:187` | score-db persist failure warn-only, no counter — these failures later surface as 1.6's silent None | counter |
| 1.11 | `kernel/decision_trace.py:186,190` | per-ticker QP delta/target silently dropped from the trace on cast failure; QP delta is a REQUIRED audit-surface field (CLAUDE.md) | sentinel + counter |
| 1.12 | `kernel/data_cache.py:113,225` | `pd.to_datetime` coercion failure → `pass` → non-datetime index flows into merge/dedupe silently | fail or counter |
| 1.13 | `kernel/pipeline/task_data_verification.py:177-180` | the data-VERIFICATION task self-skips (`return True`) when the data root can't resolve; warn-only (redundant, not dangerous: scoring fails hard on the same condition) | fail closed |
| 1.14 | `kernel/panel_pipeline/job_panel_scoring.py:2660-2663` | per-regime calibrator load failure degrades to pooled calibrator at INFO level — pooled fallback designed for *absent*, not *broken* artifacts | warn + counter; fail closed on parse error |
| 1.15 | `shadow_scoring.py:403-404` (also `task_topup.py:170`, `order_attribution.py:44,60`) | declared-outputs drift: docstring "Writes:" omits `ctx._shadow_summary`; silent neutral defaults / legacy degrades — symptom of no enforced output declaration in the framework | fix docstrings; consider a declared-outputs adapter in common |
| 1.16 | `renquant_common/pipeline.py:120-124` | `PipelineResult.ok` is hardcoded `True` — no failure path ever sets `ok=False`; the audit-record contract cannot represent a failed pipeline | plumb failure into PipelineResult (owner: common) |

## 2. Ownership violations

Charter cross-check (#210 table): admission enforcement IS where it should be
(`job_universe.py:267+ FilterStalenessTask`); model *selection* stays
config-driven (`model_registry.py` reads `ranking.panel_scoring.kind`);
umbrella→pipeline consumption (`software_stops.py`, `gate_registry`) is the
allowed direction; `_data_root.py` resolves the umbrella as *data* root
(sanctioned — the umbrella is the canonical data store), fail-fast on
sentinel. The violations are on the training and legacy-scorer paths:

### P1

| # | file:line | Finding | Rule | One-line fix | Owner |
|---|---|---|---|---|---|
| 2.1 | `pp_training.py:407,434,618,636,728,747,775,806,820`; `pp_training_full.py:199` | `from training.tournament/export/features/regime/scoring import ...` — the top-level `training` package exists NEITHER in this repo NOR in renquant-common; it resolves only from the umbrella's PYTHONPATH. Real training executes in-repo (`pp_training.py:452` `gmm.fit(...)`; TickerTournamentJob/TickerExportJob "train all model types, write models/ artifacts"). Also `model_registry.py:19-21,244` carries `train_cmd` handlers in the inference registry | forbidden import direction + "Do not train models" (CLAUDE.md) + docs/source-map.md "Do not port model training loops" | relocate TrainingPipeline to renquant-model (the factory) or delete the copy until factory cutover | renquant-model |
| 2.2 | `kernel/panel_pipeline/patchtst_scorer.py:34-38,81-82` | `_add_umbrella_scripts_to_path()` inserts `<umbrella>/scripts` into `sys.path`, then `from transformer_v4 import PatchTSTRanker` — the legacy PatchTST scorer has NO renquant-model primary path at all (contrast `hf_patchtst_scorer.py:133-170`, which tries the model package first and fails loudly) | forbidden import direction; model-family code belongs to renquant-model | route PatchTSTRanker through a renquant-model entry point; keep umbrella file-import as guarded rollback only | pipeline + renquant-model |
| 2.3 | `tests/test_import_boundaries.py:22-36` | `FORBIDDEN_ROOT_IMPORTS` omits `training` and `transformer_v4`, and the AST scan cannot see the `sys.path.insert` file-import pattern (`test_no_bare_kernel_imports.py` guards only bare `kernel`) — so 2.1/2.2 pass CI by construction | boundary enforcement gap | add `training` + `transformer_v4` to the forbidden list + a sys.path-insert grep; keep `_PHASE1_EXCLUSIONS` explicit | pipeline |

### P2

| # | file:line | Finding | One-line fix | Owner |
|---|---|---|---|---|
| 2.4 | `kernel/data.py:270-317,495-539`, `kernel/data_cache.py` | vendor ingestion in pipeline (yfinance fetch; alpaca SDK + ALPACA_API_KEY intraday bars) — `kernel/__init__.py:70-73` itself says this layer "belongs in renquant-base-data"; allowlisted as Phase-5+ debt in `test_import_boundaries.py:78`; only in-repo consumer is the (also-violating) pp_training fetch | move ingestion to base-data; keep cache-read only | base-data |
| 2.5 | `kernel/pipeline/task_execution.py:204,332`, `kernel/execution/backend_lean.py:104+` | order-placement flow hosted in pipeline (`backend.place_market_order`; LEAN `MarketOrder`/`Liquidate` — real orders in backtest/QC-paper). `pp_execution.py:11-14` documents a not-yet-active consolidation target; no live Alpaca backend in-repo | at consolidation cutover, graduate `kernel/execution/` + Execute*Tasks to renquant-execution behind the OrderIntent contract | execution |
| 2.6 | `kernel/broker_reconciliation.py:1-27` | reconciliation POLICY in pipeline (pure state machine, no broker I/O; stamps wash-clocks/quarantine). Charter assigns "cancel/reconcile/audit" to execution; the decision-relevant halves justify pipeline | document the split (policy here / executor in execution) or move the Action-executor side | pipeline (decide + document) |
| 2.7 | `kernel/intraday_wash.py:1-6` | training-panel data washing (hourly/minute features for umbrella trainers) in the runtime repo | move with the trainer to renquant-model/base-data | renquant-model |
| 2.8 | `kernel/panel_pipeline/hf_patchtst_scorer.py:133` | primary fallback targets `renquant_model_patchtst.hf_trainer` — an ARCHIVED repo (merged into renquant-model per RFC P3) | retarget to `renquant_model` entry points | pipeline |

## 3. Hand-copied implementations (single-impl-imports-only)

**M6 invariant status: HOLDS on the compare path, LEAKS on the compute
path.** The shared impl lives in `renquant_common.model_fingerprint`
(correct); `fingerprint_dispatch.py` is the only comparison decider and its
callers are exactly the three designed enforcement points
(`panel_scorer.py:161`, `walk_forward/loader.py:140`,
`job_panel_scoring.py:51`). Residuals:

| # | file:line | Finding | Sev | One-line fix | Owner |
|---|---|---|---|---|---|
| 3.1 | `kernel/walk_forward/loader.py:431` | `"sha256:" + hashlib.sha256(...).hexdigest()` hand-rolled on the **scorer identity-claim path** (`_scorer_claim_for_entry`) — byte-identical duplicate of `renquant_common.model_fingerprint.artifact_sha256` | P1 | import `artifact_sha256` | pipeline |
| 3.2 | `intraday_decisioning.py:103-131` | `compute_parent_intent_id` — BYTE-LOCKSTEP hand copy of `renquant_execution.order_state_machine.compute_parent_intent_id:177`; its own docstring says "any change must land in both repos (or better: move the one implementation to renquant-common)" — exactly the triple-impl failure shape, protected only by golden vectors | P1 | move to renquant-common; import from both | common (+pipeline, execution) |
| 3.3 | `kernel/exits.py:52-105`, `kernel/data.py:37`, `kernel/pipeline/task_data_freshness.py:204`, `kernel/typed_past/typed_data_freshness.py:77`, `kernel/execution/t2_settlement.py:28-58` | **no calendar module exists in renquant-common**; FIVE independent NYSE session-calendar wrappers. Worst: `_last_completed_nyse_close` exists TWICE with already-diverged semantics (the `task_data_freshness` copy handles today's not-yet-closed session via `now_ts`; the `typed_past` copy doesn't) ⇒ intraday runs can disagree on the freshness bar. Fallbacks: `ref − 2 calendar days` on ImportError; `exits.py:73-74` weekday-only fail-OPEN on exception | P1 (diverged pair) / P2 (consolidation) | extract one session-calendar module to renquant-common; delete the 4 copies | common + pipeline |
| 3.4 | `kernel/regime.py:97-148` | `compute_hurst`/`rolling_hurst` — verbatim copy (diff = docstring/whitespace only) of `renquant_common/hurst.py`, which was explicitly extracted as the shared canonical; `regime.py` header declares "No common/ imports" as policy. Next fix in common silently diverges the production regime detector | P1 | import `renquant_common.hurst`; retire the header policy | pipeline |
| 3.5 | `kernel/portfolio_qp/e1_tc_decomposition.py:336`, `kernel/portfolio_qp/patchtst_replay_loader.py:50` | two more private full-file-hash impls (same `sha256:` format) on replay/evidence provenance paths | P2 | import `artifact_sha256` | pipeline |
| 3.6 | `scripts/shadow_replay_bl1_recenter.py:191` | calibrator hashed as bare hexdigest (no `sha256:` prefix) — a third format; evidence `cal_sha` can never be cross-checked against artifact stamps | P2 | use `artifact_sha256` | pipeline |
| 3.7 | `kernel/artifact_resolver.py:78` | one-off `sha256[:16]` truncated audit digest (4th format; `pit_reader.py:60` same but data-only, benign) | P2 | standardize on full, prefixed `artifact_sha256` | pipeline |
| 3.8 | `tests/test_model_content_sha256_shared.py` | enforces re-export identity only — no AST/grep guard against NEW raw `hashlib` on identity paths (3.1 proves the gap; benign uses to allowlist: `broker_reconciliation.py:97`, `live_shadow_telemetry.py:111`, intraday run key) | P2 | add grep/AST guard test with allowlist | pipeline |
| 3.9 | `kernel/regime.py:352-358` vs `kernel/pipeline/pp_training.py:505` | regime-confidence formula `(hurst_rev − h)/max(hurst_rev − floor, 1e-6)` duplicated runtime vs training | P2 | share one helper | pipeline |
| 3.10 | `renquant_common/config_consistency._model_relevant_fields` vs strategy-104 `config_drift.py:12` `DEFAULT_IGNORES` | two independent "which config fields matter" significance sets; also `preflight.py:812-818` + `preflight_pipeline/tasks/config_fingerprint.py:76-83` treat ImportError of config_consistency as skip (fail-open) | P2 | single significance manifest; fail closed on import error | common + strategy-104 |
| 3.11 | `kernel/portfolio_qp/qp_solver.py:267,277,312,456` vs `kernel/portfolio_qp/cvxportfolio_backend.py:112,120,135,146` | QP numeric tolerances duplicated line-for-line (σ clip 1e-6, ridge 1e-8, dd floor 1e-6, cvar 1e-6) — solver-parity contract with no shared constants | P2 | shared `qp_tolerances` module | pipeline |

Not-findings (checked, clean): `kernel/net_safety.py` is a clean re-export of
common; `kernel/pipeline/pipeline.py` composes on
`renquant_common.run_parallel`; common's `hmm_regime_labels._compute_hurst`
vs pipeline Hurst is a *designed* split (eval-grid heuristic vs production
R/S) — the copy problem is 3.4, not this.

## 4. Flag hygiene

Rule set: flags default-OFF with byte-inertness regression tests; each flag
needs a strategy-104 config key or documented absence; no orphans.

| Flag (config path) | Pipeline reader | Default | Byte-inertness test | strategy-104 key | Verdict |
|---|---|---|---|---|---|
| `sleeve.*` (parking) | `task_parking_sleeve.py:128` | OFF | `test_parking_sleeve.py` TestDefaultOff (absent/false/non-dict inert) | PRESENT (enabled=false, mode=shadow, pinned) | CLEAN |
| `sizing.one_share_floor_enabled` | `task_selection.py:226-231` | OFF | flag-absent + explicit-false byte-identical | ABSENT (documented pipeline-side only) | P2 (4.1) |
| `execution.fractional_shares.enabled` | `kernel/sizing.py:214,243,283` (bool-strict; malformed fails closed) | OFF | `test_fractional_sizing_stage2.py::test_flag_off_byte_inert` | ABSENT (named follow-up in doc/progress s-frac §147) | P2 (4.2) |
| `ranking.panel_scoring.global_calibration.recenter_raw_per_bar` | `job_panel_scoring.py:2751-2763` | OFF | flag-off byte-identical + false==absent | ABSENT | P2 (4.3) |
| `admission_shadow.enabled` | `task_admission_shadow.py:321` | **ON** (observe-only kill switch) | decision-inert + fail-isolation + kill-switch tests | ABSENT | P2 (4.4) |
| `execution.software_stops.enabled` | `software_stops.py:293-294` | OFF | `TestFlagOffInert` | ABSENT, no documented key plan | P2 (4.5) |
| `intraday_decisioning.enabled` | `intraday_decisioning.py:427-432` | OFF | `test_flag_defaults_off_and_disabled_tick_is_inert` | PRESENT (true + mode=shadow pinned; Stage-2 bar = deliberate test rewrite) | CLEAN |
| `ranking.panel_scoring.fingerprint.accept_legacy_stamps` | `fingerprint_dispatch.py` | TRUE (documented M6 migration window) | default-true + key-read tests (`test_fingerprint_version_dispatch.py:104-115`) | PRESENT (true, pinned; flip = step-4 act) | CLEAN |
| `model_staleness_days` | `job_universe.py:302-304` | **0 ⇒ gate silently OFF** | NO missing-key test | PRESENT (60) | **P1** — see 5.7 |

Findings:

- **4.1-4.3 (P2)** — `one_share_floor`, `fractional_shares`,
  `recenter_raw_per_bar`: default-OFF with exemplary inertness tests, but
  the policy key was never declared in strategy-104. The repo's own newer
  convention (sleeve #39, intraday #41, fingerprint-key PR) is to declare
  inert keys in policy so activation is a reviewable config PR. Fix: land
  the three default-false keys + pin tests. Owner: strategy-104.
- **4.4 (P2)** — `admission_shadow` is the one **default-ON** flag, with no
  strategy-104 key: observe-only, fail-isolated, decision-inert by test —
  but the default-ON exemption is documented only pipeline-side. Fix:
  declare the key in strategy-104 with the standard pin test. Owner:
  strategy-104.
- **4.5 (P2)** — `execution.software_stops.enabled`: default-OFF + inertness
  test, but no config key and no documented key plan (contrast every other
  dark flag). Fix: declare the default-false key or document
  absent-by-design. Owner: strategy-104.
- **4.6 Orphan / umbrella-only keys (P2)** — set in strategy-104, read by NO
  code in pipeline/orchestrator/execution/backtesting/model/common:
  `inference_frame_cache.enabled` (only reader: umbrella
  `backtesting/renquant_104/training_panel/pipeline.py`) and
  `wf_gate.benchmark_required` / `wf_gate.regime_required` (only reader:
  umbrella `scripts/run_wf_gate.py`). Not dead keys — but their only readers
  are unmigrated umbrella code, i.e. per the #210 table the contracts'
  owners (backtesting/model) don't own their readers yet. Owner: umbrella
  migration (backtesting/model).
- **Lockstep verdict: healthy.** active==golden semantic match
  (`test_active_and_golden_semantic_config_match`, provenance-stripped) +
  watchlist match + `config_drift.py` "flag quietly enabled" detector.
  Shadow deliberately diverges (scorer kind) with per-key safety pins
  iterating all three configs. One carve-out is itself a finding (5.9).

## 5. Contract duplication / divergent safe-defaults (the raw>0 / #140 class)

The dangerous pattern is not two configs disagreeing — it is pipeline
hardcoding a fallback default that **contradicts the strategy-104 value for
the same key**, so losing the key (side-config, replay config, sim config,
refactor) silently flips live semantics. Verified value-by-value against
`configs/strategy_config.json`:

| # | file:line | Key | Pipeline default | Config value | Sev / fix / owner |
|---|---|---|---|---|---|
| **5.1** | `job_panel_scoring.py:3610` + `preflight.py:1471-1476` | `ranking.kelly_sizing.sigma_horizon_days` | **252.0**, and preflight P-KELLY-SIGMA-HORIZON **PASSES** on the absent key ("using default 252") | 60 — with an in-config post-mortem explaining 252 WAS the 2026-06-11 bug (~4.2x variance inflation, high-vol names crushed) | **P0 (latent)** — absent key ⇒ preflight green + the documented bug silently re-armed. Fix: absent-key branch FAILS when kelly+use_calibrator_mu are on; kill the 252 runtime default. Owner: pipeline |
| 5.2 | `job_panel_scoring.py:3653,3655` | `kelly_sizing.fractional` / `max_concentration` | 0.25 / **0.35** | 0.3 / **0.12** | P1 — ~3x looser concentration cap on key loss. Fix: no numeric default; fail closed. Owner: pipeline |
| 5.3 | `task_topup.py:212` | `topup_conviction_floor` | **0.20** | 0.55 | P1 — topup admission bar silently collapses. Owner: pipeline |
| 5.4 | `rotation.py:447` + `task_rotation.py:89` | `min_expected_advantage_pct` | **0.03** (hand-copied in two files) | 0.06 | P1 — rotation bar halves on key loss. Owner: pipeline |
| 5.5 | `portfolio_qp/tasks.py:477,482,2696,2702,3506` | `qp_sigma_horizon_mode` / `qp_sigma_unit` / `qp_horizon_contract` | "none" / "horizon" / **"warn"** | "match_mu" / "annualized" / **"strict"** | P1 — the σ-horizon enforcement stack fails OPEN if the joint_actions subtree is missing. Owner: pipeline |
| 5.6 | `trade_events.py:542` | `qp_tax_lot_method` | **"fifo"** | "hifo" | P1 — lot selection (tax economics) silently flips on key loss. Owner: pipeline |
| 5.7 | `job_universe.py:302-304` | `model_staleness_days` | **0 ⇒ staleness admission gate OFF** | 60 | P1 — violates "do not silently continue on stale artifact/data fingerprints" (#210 admission enforcement); no missing-key test (§4). Fix: absent key = fail closed (required key). Owner: pipeline |
| 5.8 | `trade_events.py:330-332`, `rotation.py:134-136,299-301,478-480`, `task_joint_actions.py:140-142` | tax-rate triple (0.50/0.32/365) | hand-copied at 5 call sites in 3 files (matches config today) | same | P2 — one edited copy desynchronizes sell vs rotation vs joint tax math. Fix: one `tax_params(config)` helper. Owner: pipeline |
| 5.9 | `configs/strategy_config.json:1305`, `strategy_config.shadow.json:1206` (+ `tests/test_strategy_configs.py:51-57` pins it) | `walkforward.manifest_path` | — | **ABSOLUTE developer-local umbrella path** (`/Users/renhao/git/github/RenQuant/...`), no fingerprint pin; `_require_relative_path` guards only panel_scoring paths | P1 — Universal Rule 4 violation baked into the semantic-match carve-out. Fix: repo-relative/URI + fingerprint; extend `_require_relative_path`; update the pin test. Owner: strategy-104 (+ umbrella publishes the manifest portably) |
| 5.10 | `task_sell.py:381` vs `task_rotation.py:105` / `task_joint_actions.py:218`; `task_joint_actions.py:335`; `exits.py:925,934` | `panel_sell_floor` (0.20 hardcoded vs None-checked), `panel_buy_top_n` (3), `consecutive_sell_signals` (3, twice) | inconsistent same-key handling; values match config today | same | P2 — unify on None-check/required. Owner: pipeline |
| 5.11 | `config_drift.py:72-75` | — | `resolve_config_root` prefers umbrella `repo_root/backtesting/<strategy>` copies over the repo's own `configs/` | — | P2 — documented transition (CLAUDE.md) but inverts source-of-truth. Owner: strategy-104 |

## 6. Dead code / twin modules

Clarification first (so nobody "cleans up" the wrong stack): the top-level
contract modules (`inference.py`, `panel_scoring.py`, `selection.py`,
`model_admission.py`, `runtime_features.py`) are NOT dead — they are the
rq105/intraday + native-inference public API (`__init__.py` exports;
`intraday_decisioning.py:70-72` composes them). The repo deliberately hosts
two decision stacks (lifted-kernel 104 batch vs contract-based 105). But
that means sell/selection/trace logic exists twice at different maturities —
which produced 6.1/6.2:

| # | file:line | Finding | Sev | One-line fix | Owner |
|---|---|---|---|---|---|
| 6.1 | `src/renquant_pipeline/decision_trace.py:13,45` vs `kernel/decision_trace.py:13,61` | TWO live implementations of `model_type_from_artifact` / `active_scorer_identity` with different signatures/bodies (272 vs 401 lines) — attribution identity computed two ways = the calibrator triple-impl bug class | P1 | make one canonical + re-export shim (the pattern `kernel/pipeline/context.py` already uses correctly) | pipeline |
| 6.2 | `src/renquant_pipeline/state_paths.py` vs `kernel/state_paths.py` | byte-IDENTICAL twins (verified `cmp`), BOTH live (top-level: `software_stops.py:111`, `__init__.py:59`; kernel: `job_universe.py:402,552`, `preflight.py:1236`) — first divergent edit silently forks broker-isolation path logic (the 2026-04-27 paper-contaminates-alpaca guard) | P1 | keep one, shim the other; parity test meanwhile | pipeline |
| 6.3 | `kernel/panel_pipeline/alpha158_features.py:13-16,41+` | docstring claims "both build script and this module import the same low-level functions" (the named anti-skew invariant) but the module imports only numpy/pandas and hand-mirrors the qlib operators; the umbrella's `build_alpha158_qlib.py` is a separate hand-maintained impl ⇒ **unenforced train/serve feature parity on the live XGB primary path** (`job_panel_scoring.py:1235-1251` calls `compute_alpha158_at` for live inference) | P1 | extract shared operators to renquant-common OR add a parity golden test vs the training builder; fix the false docstring | common + pipeline (+ model for the builder) |
| 6.4 | `kernel/panel_pipeline/ensemble_scorer.py` | zero src consumers; not registered in `model_registry.py` (registered kinds: xgb, patchtst, hf_patchtst, regime_router); ensemble was SHELVED by the 2026-06-12 scorer-lineup decision | P2 | delete or mark shelved-with-reopening-trigger | pipeline |
| 6.5 | `kernel/pit_reader.py`, `kernel/score_audit.py`, `kernel/typed_past/typed_data_freshness.py`, `kernel/data_coverage.py` | graduated-but-unwired: zero in-src consumers; reachable only from their own tests (data_coverage's documented consumers are umbrella scripts) — deployed-but-dark | P2 | wire into a composition or move to the owning repo; don't count as shipped | pipeline / orchestrator |
| 6.6 | `kernel/execution/backend.py:12-14` | claims a CI grep over `adapters/` that does not exist in this repo (nothing in ci.yml/Makefile runs it) — stale enforcement claim | P2 | fix or delete the claim | pipeline |

## Appendix: positive controls (verified conform — keep doing this)

- `kernel/pipeline/pipeline.py` — canonical re-export of common primitives +
  config-default adapter; collapsed the old duplicate executor.
- `preflight_pipeline/base.py:44-73` — exception → fail-closed check,
  sell-only soft-exempt, every gate always evaluated.
- `task_admission_shadow.py:335-339`,
  `portfolio_qp/live_shadow_telemetry.py:404-407` — catch-all + counter +
  stamp: the correct observe-only pattern.
- `job_panel_scoring.py` scoring path — all scorer failures route through
  `_fail_closed_panel_scoring` with named reasons.
- QP failure counters (`portfolio_qp/tasks.py:123-132`).
- `kernel/sizing.py` fractional config read — bool-strict; malformed config
  fails CLOSED to whole-share mode.
- Admission enforcement located per the #210 ownership table
  (`job_universe.py FilterStalenessTask`, two-axis, fail-closed for
  offensive buys, held-name exit path preserved).
- strategy-104 pin-test discipline (`test_strategy_configs.py`) — inert-flag
  pins, operator-override audit manifest, semantic active==golden match,
  `config_drift.py` flag-quietly-enabled detection.
- M6 compare-path invariant holds: `fingerprint_dispatch` is the only
  comparison decider; shared `model_content_sha256` lives in common with
  identity-pinned re-exports.

## Suggested sequencing (no code in this PR)

1. **Now (small, high-leverage):** 5.1 preflight fail-on-absent +
   kill the 252 default; 1.5 shadow try-scope; 2.3 boundary-test additions;
   6.2 state_paths shim; 3.1 `artifact_sha256` import.
2. **Next:** the §5 divergent-default sweep (one PR: remove
   contradicting numeric defaults, fail closed); 6.1 decision-trace
   canonicalization; 3.3 calendar extraction to common (unblocks 3.3's
   diverged pair + 1.13/freshness parity).
3. **With the factory cutover:** 2.1/2.2 training + legacy-scorer
   relocation; 2.4/2.5 ingestion/execution graduation.
4. **Strategy-104 config PRs:** 4.1-4.5 declare the dark-flag keys;
   5.9 portable walkforward manifest.
