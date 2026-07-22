"""Shadow model scoring — record alt-model decisions via MLflow tracking.

Per 2026-05-18 user request: inference accepts shadow models that run
the full pipeline but DON'T submit orders; all data recorded for review.

Per 2026-05-18 second update: use 3rd-party library (MLflow) instead of
custom SQLite — battle-tested experiment tracking, built-in UI for
comparison, standard schema.

MLflow tracking layout:
  experiment_name = ranking.panel_scoring.shadow_experiment
                    (default: "renquant_104_shadow")
  per inference: ONE MLflow Run per (date, shadow_model)
    tags:    as_of_date / shadow_name / shadow_kind / primary_kind
    metrics: mean_primary_score / mean_shadow_score / mean_diff
             corr_primary_shadow / rank_agreement_top5 / top5_overlap
    artifact: comparison.csv (per-ticker primary vs shadow scores)

Query later (UI or programmatic):
  $ mlflow ui --backend-store-uri file:./mlruns
  → http://127.0.0.1:5000 → experiment → compare runs

  # Or programmatic:
  import mlflow
  exp = mlflow.get_experiment_by_name("renquant_104_shadow")
  runs = mlflow.search_runs(exp.experiment_id, filter_string="tags.shadow_name='patchtst_v1'")
  print(runs[["start_time", "metrics.mean_diff", "metrics.corr_primary_shadow"]])

Why MLflow over custom SQLite:
  - Standard schema, well-documented
  - Built-in comparison UI
  - Run-level filtering/aggregation
  - Artifact storage (per-bar comparison tables)
  - 3rd-party maintained, battle-tested in production
  - No new dependency (mlflow 3.12.0 already installed)

Tests in tests/test_shadow_scoring.py.
"""
from __future__ import annotations
import datetime
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from renquant_pipeline.kernel.pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.pipeline import Job, Task
# Canonical, pure shadow health + artifact-identity contract. Lives in its own
# stdlib-only module so the three consumers that MUST NOT DRIFT — this task
# (EMIT), the shadow-artifact CI gate (orchestrator #525), and the shadow-health
# sentinel (orchestrator #566) — resolve the same ref to the same file, stamp
# the same content digest, and compute the same expected-skip-vs-fault verdict.
# Re-exported below for back-compat; shadow_health is the home.
from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
    SHADOW_HEALTH_SCHEMA,
    STATE_DISABLED,
    STATE_NO_CANDIDATES,
    STATE_NO_SHADOW_MODELS,
    append_shadow_health,
    content_digest,
    finalize_shadow_health,
    mark_expected_skip,
    new_shadow_health,
    resolve_artifact_identity,
    shadow_health_cfg,
    shadow_health_log_path,
    shadow_health_sink_defined,
)

log = logging.getLogger("kernel.panel_pipeline.shadow_scoring")

# OMP fix per [[concurrency_resource_budget]]: ensure single-thread BEFORE
# any torch model construction in shadow scorers (PatchTST etc).
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

# Default MLflow tracking URI: file-based local store at <umbrella>/mlruns.
# Lazy resolution — module import does not require the umbrella to be
# locatable; only callers of _ensure_mlflow_setup() with no tracking_uri arg.
def _default_tracking_uri() -> str:
    from renquant_pipeline.kernel.panel_pipeline._data_root import data_root  # noqa: PLC0415
    return "file:" + str(data_root() / "mlruns")
_DEFAULT_EXPERIMENT = "renquant_104_shadow"
_SCORER_CACHE: dict[tuple[str, str], object] = {}

def _resolve_shadow_artifact_path(
    artifact_path: str | Path,
    *,
    strategy_dir: str | Path | None,
    repo: Path | None = None,
) -> Path:
    """DEPRECATED thin wrapper — kept only for back-compat callers.

    Artifact resolution now goes through the ONE authority
    ``resolve_artifact_identity`` (see ``ApplyShadowScoringTask.run``); this
    delegates to it so no second, independently-resolved path can diverge from
    the identity the health record certifies (codex CR#2). It preserves the
    established resolution order (absolute → strategy_dir → repo_root). Returns
    the resolved file path when the ref resolves, else the best-effort located
    candidate. New code should call ``resolve_artifact_identity`` directly.
    """
    identity = resolve_artifact_identity(
        artifact_path, strategy_dir=strategy_dir, repo_root=repo)
    if identity.resolved_path is not None:
        return Path(identity.resolved_path)
    return Path(artifact_path)


def _is_degenerate_cross_section(
    sub: "pd.DataFrame", *, zero_var_threshold: float = 1e-12, frac_hard: float = 0.5
) -> bool:
    """True when >``frac_hard`` of columns have ~zero cross-sectional variance.

    Mirrors ``model_contract``'s input HARD-FAIL condition (abs(std) <
    ``zero_var_threshold`` over >50% of cols). Used to skip a NON-history shadow
    scorer that was handed a target-only/degenerate ``ctx._panel_matrix`` — which
    is what happens on history-primary runs (e.g. hf_patchtst): no valid
    per-ticker cross-section is stamped for non-history scorers, so every name
    gets a constant input and the scorer would collapse (model_contract HARD
    FAIL). Needs ≥2 rows to assess variance.
    """
    if sub is None or len(sub) < 2 or sub.shape[1] == 0:
        return False
    col_stds = sub.std(axis=0, skipna=True).fillna(0.0)
    return float((col_stds.abs() < zero_var_threshold).mean()) > frac_hard


def _ensure_mlflow_setup(tracking_uri: Optional[str] = None,
                         experiment_name: Optional[str] = None) -> str:
    """Set MLflow tracking URI + experiment. Returns experiment_id."""
    import mlflow  # noqa: PLC0415
    mlflow.set_tracking_uri(tracking_uri or _default_tracking_uri())
    name = experiment_name or _DEFAULT_EXPERIMENT
    exp = mlflow.get_experiment_by_name(name)
    if exp is None:
        exp_id = mlflow.create_experiment(name)
    else:
        exp_id = exp.experiment_id
    return exp_id


def _log_shadow_run(experiment_id: str, as_of_date, shadow_name: str,
                    shadow_kind: str, primary_kind: str,
                    primary_scores: dict[str, float],
                    shadow_scores: dict[str, float],
                    primary_ranks: dict[str, int],
                    shadow_ranks: dict[str, int]) -> None:
    """Persist one shadow run's comparison to MLflow."""
    import mlflow  # noqa: PLC0415

    # Aggregate metrics
    tickers = sorted(set(primary_scores) & set(shadow_scores))
    if not tickers:
        return
    ps = np.array([primary_scores[t] for t in tickers])
    ss = np.array([shadow_scores[t] for t in tickers])
    diffs = ss - ps
    # Rank agreement: how many of top-5 primary are in top-5 shadow
    n_top = min(5, len(tickers))
    top_primary = sorted(primary_ranks.items(), key=lambda x: x[1])[:n_top]
    top_shadow = sorted(shadow_ranks.items(), key=lambda x: x[1])[:n_top]
    top_primary_set = {t for t, _ in top_primary}
    top_shadow_set = {t for t, _ in top_shadow}
    overlap = len(top_primary_set & top_shadow_set)
    # Pearson correlation
    if np.std(ps) > 1e-9 and np.std(ss) > 1e-9:
        corr = float(np.corrcoef(ps, ss)[0, 1])
    else:
        corr = float("nan")

    run_name = f"{shadow_name}_{as_of_date}"
    with mlflow.start_run(experiment_id=experiment_id, run_name=run_name):
        mlflow.set_tags({
            "as_of_date": str(as_of_date),
            "shadow_name": shadow_name,
            "shadow_kind": shadow_kind,
            "primary_kind": primary_kind,
            "n_candidates": str(len(tickers)),
        })
        mlflow.log_metrics({
            "mean_primary_score": float(np.mean(ps)),
            "mean_shadow_score": float(np.mean(ss)),
            "mean_diff": float(np.mean(diffs)),
            "std_diff": float(np.std(diffs)),
            "corr_primary_shadow": corr,
            f"top{n_top}_overlap": float(overlap),
            f"top{n_top}_overlap_pct": float(overlap / n_top) if n_top else 0.0,
        })
        # Per-ticker comparison table as artifact
        comparison = pd.DataFrame({
            "ticker": tickers,
            "primary_score": ps,
            "shadow_score": ss,
            "diff": diffs,
            "primary_rank": [primary_ranks.get(t, -1) for t in tickers],
            "shadow_rank": [shadow_ranks.get(t, -1) for t in tickers],
            "rank_diff": [shadow_ranks.get(t, 0) - primary_ranks.get(t, 0)
                          for t in tickers],
        })
        # MLflow log_table writes to artifacts/<artifact_file>
        mlflow.log_table(comparison, "comparison.json")


class ApplyShadowScoringTask(Task):
    """Run each configured shadow model on the SAME candidates as primary,
    record scores via MLflow tracking. Read-only — no order submission.

    Reads:
      - ctx.candidates (with .panel_score set by main)
      - ctx.config["ranking"]["panel_scoring"]["shadow_models"]
      - ctx.config["ranking"]["panel_scoring"]["shadow_tracking_uri"]
        (default: file:<repo>/mlruns)
      - ctx.config["ranking"]["panel_scoring"]["shadow_experiment"]
        (default: "renquant_104_shadow")

    Writes:
      - MLflow run per shadow model per inference bar

    Soft-fail: shadow errors logged, primary pipeline unaffected.
    """

    name = "ApplyShadowScoringTask"

    def run(self, ctx: InferenceContext) -> bool | None:
        panel_cfg = ctx.config.get("ranking", {}).get("panel_scoring", {})

        # ── Shadow HEALTH RECORD wiring (silent-failure sentinel feed) ──────
        # Set up BEFORE any early return so a record is emitted on every path —
        # a by-design non-run (disabled / no models / no candidates) is an
        # EXPECTED skip (actionable=True), NOT silence the sentinel must guess
        # at. Normalize the session date once (ctx.today may be pd.Timestamp /
        # datetime / date). shadow_health.enabled=false is a health-only kill
        # switch (never disables the shadow scoring itself).
        strategy_dir = ctx.config.get("_strategy_dir")
        today_val = getattr(ctx, "today", datetime.date.today())
        if isinstance(today_val, datetime.datetime):
            run_date = today_val.date()
        elif isinstance(today_val, datetime.date):
            run_date = today_val
        else:
            try:
                run_date = pd.Timestamp(today_val).date()
            except Exception:  # noqa: BLE001
                run_date = datetime.date.today()
        run_id = getattr(ctx, "run_id", None)
        hcfg = shadow_health_cfg(ctx.config)
        health_enabled = bool(hcfg.get("enabled", True))
        try:
            max_staleness_days = int(hcfg.get(
                "max_staleness_days", DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS))
        except (TypeError, ValueError):
            max_staleness_days = DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS
        try:
            min_coverage_frac = float(hcfg.get(
                "min_coverage_frac", DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC))
        except (TypeError, ValueError):
            min_coverage_frac = DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC

        def _flush(records: "list[dict[str, Any]]") -> None:
            """Finalize (idempotent) + append each record to the JSONL sink.
            Best-effort: NEVER fail the (already non-fatal) shadow task."""
            if not records:
                return
            for rec in records:
                try:
                    finalize_shadow_health(
                        rec, run_date=run_date,
                        max_staleness_days=max_staleness_days,
                        min_coverage_frac=min_coverage_frac)
                except Exception:  # noqa: BLE001
                    log.exception("ApplyShadowScoringTask: health finalize failed")
            if not shadow_health_sink_defined(ctx.config):
                log.debug(
                    "ApplyShadowScoringTask: %d shadow health record(s) not "
                    "persisted (no _strategy_dir / shadow_health.path)",
                    len(records))
                return
            try:
                sink = shadow_health_log_path(ctx.config)
                for rec in records:
                    append_shadow_health(sink, rec)
                n_fault = sum(1 for r in records if not r.get("actionable"))
                log.info(
                    "ApplyShadowScoringTask: wrote %d shadow health record(s) "
                    "to %s (%d fault)", len(records), sink, n_fault)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ApplyShadowScoringTask: shadow health write failed: %s", exc)

        def _skip_record(sm: dict, state: str, reason: str,
                         n_candidates: int) -> "dict[str, Any]":
            rec = new_shadow_health(
                shadow_name=(sm.get("name", "unnamed_shadow") if sm else None),
                kind=(sm.get("kind") if sm else None),
                artifact_path=(sm.get("artifact_path") if sm else None),
                run_date=run_date, run_id=run_id, n_candidates=n_candidates,
                expected_content_sha256=(sm.get("expected_content_sha256") if sm else None),
                expected_config_fingerprint=(sm.get("expected_config_fingerprint") if sm else None),
            )
            return mark_expected_skip(rec, state, reason)

        shadow_models = panel_cfg.get("shadow_models", []) or []

        # Task-level EXPECTED skips — emit a record, then return.
        if panel_cfg.get("shadow_enabled", True) is False:
            if health_enabled:
                _flush([_skip_record({}, STATE_DISABLED, "shadow_enabled=false", 0)])
            return None
        if not shadow_models:
            if health_enabled:
                _flush([_skip_record({}, STATE_NO_SHADOW_MODELS,
                                     "no shadow_models configured", 0)])
            return None

        # Primary scores (must be set by main ApplyScoresTask)
        cands = list(ctx.candidates) if ctx.candidates else []
        primary_scores = {c.ticker: float(c.panel_score)
                          for c in cands if c.panel_score is not None}
        if not cands or not primary_scores:
            # Nothing to compare against this run — an EXPECTED skip, one record
            # per configured shadow so the per-shadow timeline stays continuous.
            reason = "no_candidates" if not cands else "no_primary_scores"
            log.info("ApplyShadowScoringTask: %s — skip", reason)
            if health_enabled:
                _flush([_skip_record(sm, STATE_NO_CANDIDATES, reason,
                                     len(primary_scores)) for sm in shadow_models])
            return None
        sorted_primary = sorted(primary_scores.items(), key=lambda x: -x[1])
        primary_ranks = {t: i + 1 for i, (t, _) in enumerate(sorted_primary)}
        primary_kind = panel_cfg.get("kind", "xgb")

        # MLflow tracking setup — a setup failure DISABLES shadow MLflow logging
        # but does NOT skip the run: the health record is still assessed (the
        # tracking sink is orthogonal to shadow health).
        shadow_log_mlflow = bool(panel_cfg.get("shadow_log_mlflow", True))
        exp_id = None
        if shadow_log_mlflow:
            try:
                exp_id = _ensure_mlflow_setup(
                    panel_cfg.get("shadow_tracking_uri"),
                    panel_cfg.get("shadow_experiment"))
            except Exception as exc:
                log.warning("ApplyShadowScoringTask: MLflow setup failed: %s — "
                             "disabling shadow MLflow logging (health still assessed)",
                             exc)
                shadow_log_mlflow = False

        from renquant_pipeline.kernel.panel_pipeline.model_registry import registry  # noqa: PLC0415
        # Artifact resolution is delegated per-shadow to the canonical
        # resolve_artifact_identity below — the ONE authority the #525 CI gate +
        # #566 sentinel also consume — so the file scoring loads and the identity
        # the record certifies never diverge (codex CR#2). The umbrella repo_root
        # fallback is derived (from strategy_dir) lazily inside the resolver.
        health_records: list[dict[str, Any]] = []

        for sm in shadow_models:
            name = sm.get("name", "unnamed_shadow")
            kind = sm.get("kind")
            artifact_path = sm.get("artifact_path")
            health = new_shadow_health(
                shadow_name=name, kind=kind, artifact_path=artifact_path,
                run_date=run_date, run_id=run_id,
                n_candidates=len(primary_scores),
                expected_content_sha256=sm.get("expected_content_sha256"),
                expected_config_fingerprint=sm.get("expected_config_fingerprint"),
            )
            try:
                if not kind or not artifact_path:
                    health["load_error"] = "missing kind/artifact_path"
                    log.warning("ApplyShadowScoringTask: shadow %s missing "
                                 "kind/artifact_path", name)
                    continue
                # SINGLE canonical resolution + immutable identity. This is the
                # ONE authority the shadow-artifact CI gate (#525) + health
                # sentinel (#566) also consume: the file scoring LOADS, the digest
                # the record certifies, the resolved path, and the source label
                # ALL come from this one result. No second, independently-resolved
                # path can diverge from the certified identity (codex CR#2 — the
                # loader used a separately-resolved path while the record hashed
                # another, so a record could certify one file while a DIFFERENT one
                # was actually scored).
                identity = resolve_artifact_identity(
                    artifact_path, strategy_dir=strategy_dir)
                health["artifact_resolved_path"] = identity.resolved_path
                health["artifact_source"] = identity.source
                # Immutable content identity of the file scoring will load: the
                # sha256 of its bytes, read via the canonical resolver (NOT via the
                # mtime/size-keyed content_digest cache) — if the artifact is
                # swapped the digest changes and the record stops reading "healthy".
                # None == the ref did not resolve to a real file (the ``../../``
                # class).
                health["content_sha256"] = identity.content_sha256
                health["artifact_resolved"] = identity.resolved

                # Unresolved / missing artifact → the record is a FAULT via the
                # not-loaded finalize path. Do NOT fall through to path-existence
                # or load a scorer against a path the identity did not certify.
                if not identity.resolved:
                    health["load_error"] = (
                        f"artifact_path {artifact_path!r} did not resolve to an "
                        f"existing file: {identity.error}")
                    log.warning(
                        "ApplyShadowScoringTask: shadow %s artifact unresolved: %s",
                        name, identity.error)
                    continue

                # Load from the SAME path the identity certifies.
                p = Path(identity.resolved_path)
                try:
                    handler = registry.get(kind)
                except ValueError as exc:
                    health["load_error"] = str(exc)
                    log.warning("ApplyShadowScoringTask: %s", exc)
                    continue

                # Inject shadow's feature_cols + seq_len + regime_router into config copy
                shadow_panel_cfg = dict(panel_cfg)
                if "feature_cols" in sm:
                    shadow_panel_cfg["feature_cols"] = sm["feature_cols"]
                if "seq_len" in sm:
                    shadow_panel_cfg["seq_len"] = sm["seq_len"]
                if "regime_router" in sm:  # composite scorer sub-config
                    shadow_panel_cfg["regime_router"] = sm["regime_router"]
                shadow_cfg = dict(ctx.config)
                shadow_cfg.setdefault("ranking", {})["panel_scoring"] = shadow_panel_cfg

                cache_key = (kind, str(p))
                scorer = _SCORER_CACHE.get(cache_key)
                if scorer is None:
                    try:
                        scorer = handler.scorer_loader(p, shadow_cfg)
                    except Exception as exc:
                        # The identity certified a real file (we only reach here
                        # when identity.resolved), so a raise is a genuine load
                        # failure → STATE_LOAD_FAILED via finalize.
                        health["load_error"] = str(exc)
                        log.warning("ApplyShadowScoringTask: shadow %s (%s) load failed: %s",
                                     name, kind, exc)
                        continue
                    _SCORER_CACHE[cache_key] = scorer

                # Scorer available — the shadow LOADED. Stamp provenance/identity
                # from its metadata so a stale/unfingerprinted shadow is visible.
                health["loaded"] = True
                _meta = getattr(scorer, "metadata", {}) or {}
                if isinstance(_meta, dict):
                    health["effective_train_cutoff_date"] = _meta.get(
                        "effective_train_cutoff_date")
                    health["config_fingerprint"] = _meta.get("config_fingerprint")

                target_tickers = list(primary_scores.keys())
                try:
                    if getattr(scorer, "requires_history", False):
                        # 2026-06-10 FROZEN-SCORE FIX (shadow path). Same bug as
                        # the primary ApplyScoresTask path: lazy-loading the
                        # sequence panel from the STATIC training parquet
                        # (max date 2026-02-10) and slicing `date < today` froze
                        # shadow scores for every live date past the parquet's
                        # last bar. Build the sequence from live ctx.ohlcv ending
                        # at `today` via the shared helper; only fall back to the
                        # static parquet for in-range (sim) dates.
                        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (  # noqa: PLC0415
                            _build_live_panel_history,
                        )
                        today_ts = pd.Timestamp(getattr(ctx, "today",
                                                          datetime.date.today()))
                        panel_history = getattr(ctx, "_panel_history", None)
                        if panel_history is None:
                            panel_history = _build_live_panel_history(
                                ctx, scorer, target_tickers, today_ts,
                            )
                        if panel_history is None:
                            panel_parquet = (repo / "data"
                                              / "alpha158_291_fundamental_dataset.parquet")
                            full = pd.read_parquet(panel_parquet)
                            full["date"] = pd.to_datetime(full["date"])
                            if today_ts > full["date"].max():
                                health["skip_reason"] = "no_live_history_past_static_max"
                                log.warning(
                                    "ApplyShadowScoringTask: shadow %s has no live "
                                    "OHLCV and as-of %s is past static panel max "
                                    "%s — skipping (refusing stale frozen shadow "
                                    "scores).",
                                    name, today_ts.date().isoformat(),
                                    full["date"].max().date().isoformat())
                                continue
                            past = full[full["date"] < today_ts]
                            dates = sorted(past["date"].unique())[-scorer.seq_len:]
                            panel_history = past[past["date"].isin(dates)]
                        # If scorer accepts current_regime (RegimeRouterScorer), pass it
                        import inspect as _inspect  # noqa: PLC0415
                        sig = _inspect.signature(scorer.score_with_history)
                        if "current_regime" in sig.parameters:
                            series = scorer.score_with_history(
                                panel_history, target_tickers,
                                current_regime=getattr(ctx, "regime", "BULL_CALM"))
                        else:
                            series = scorer.score_with_history(
                                panel_history, target_tickers)
                    else:
                        X = getattr(ctx, "_panel_matrix", None)
                        if X is None or X.empty:
                            health["skip_reason"] = "panel_matrix_empty"
                            log.warning("ApplyShadowScoringTask: shadow %s needs "
                                         "matrix but ctx._panel_matrix empty", name)
                            continue
                        fc = scorer.feature_cols
                        missing = [c for c in fc if c not in X.columns]
                        if missing:
                            health["skip_reason"] = (
                                "missing_feature_cols:" + ",".join(missing[:5]))
                            log.warning("ApplyShadowScoringTask: shadow %s missing "
                                         "cols: %s", name, missing[:5])
                            continue
                        sub = X[fc]
                        # 2026-06-26: when the PRIMARY scorer is history-based (e.g.
                        # hf_patchtst), ctx._panel_matrix is a target-only/degenerate
                        # placeholder (see job_panel_scoring BUG #6) — no valid
                        # per-ticker cross-section is stamped for non-history scorers.
                        # A non-history (xgb) shadow then receives a constant input
                        # across the cross-section → collapsed prediction, which trips
                        # model_contract's HARD FAIL. That is a meaningless comparison,
                        # not a model fault: skip cleanly rather than emit a false
                        # alarm. (Live impact nil — shadow-only.)
                        if _is_degenerate_cross_section(sub):
                            health["skip_reason"] = "degenerate_cross_section"
                            log.warning(
                                "ApplyShadowScoringTask: shadow %s skipped — degenerate "
                                "cross-section (%d feature cols ~constant; primary is "
                                "history-based so no panel matrix was built for "
                                "non-history shadows)", name, len(fc))
                            continue
                        series = scorer.score(sub.fillna(0))
                except Exception as exc:
                    health["skip_reason"] = f"score_error:{exc}"
                    log.warning("ApplyShadowScoringTask: shadow %s score failed: %s",
                                 name, exc)
                    continue

                shadow_dict = series.to_dict()
                # Coverage of the candidate cross-section by FINITE shadow scores
                # (a shadow that scores NaN for everyone is a silent failure too).
                _finite = {t: v for t, v in shadow_dict.items()
                           if isinstance(v, (int, float))
                           and not isinstance(v, bool)
                           and math.isfinite(float(v))}
                health["n_scored"] = len(_finite)
                health["coverage_frac"] = (
                    len(_finite) / health["n_candidates"]
                    if health["n_candidates"] else None)
                sorted_shadow = sorted(shadow_dict.items(), key=lambda x: -x[1])
                shadow_ranks = {t: i + 1 for i, (t, _) in enumerate(sorted_shadow)}

                # 2026-05-19 (user mandate "want to know what shadow will do in
                # ntfy"): stash a compact summary on ctx so live.runner can
                # surface it. Single-line-of-ntfy friendly: shadow top-3 picks,
                # top-10 overlap with primary, Spearman rank correlation.
                try:
                    import numpy as _np  # noqa: PLC0415
                    top10_primary = set(t for t, _ in sorted_primary[:10])
                    top10_shadow = set(t for t, _ in sorted_shadow[:10])
                    overlap = len(top10_primary & top10_shadow)
                    common = sorted(set(primary_scores) & set(shadow_dict))
                    if len(common) >= 5:
                        pr = _np.array([primary_ranks[t] for t in common])
                        sr = _np.array([shadow_ranks[t] for t in common])
                        from scipy.stats import spearmanr as _sp  # noqa: PLC0415
                        rho, _ = _sp(pr, sr)
                        rho = float(rho) if _np.isfinite(rho) else float("nan")
                    else:
                        rho = float("nan")
                    top3 = [t for t, _ in sorted_shadow[:3]]
                    summary = {
                        "name": name, "kind": kind,
                        "top3": top3,
                        "top10_overlap": overlap,
                        "n_candidates": len(shadow_dict),
                        "spearman_vs_primary": rho,
                    }
                    if not hasattr(ctx, "_shadow_summary"):
                        ctx._shadow_summary = []  # noqa: SLF001
                    ctx._shadow_summary.append(summary)  # noqa: SLF001
                except Exception as exc:
                    log.warning("ApplyShadowScoringTask: ctx summary failed for %s: %s",
                                 name, exc)

                if not shadow_log_mlflow or exp_id is None:
                    log.info("ApplyShadowScoringTask: shadow %s (%s) scored %d "
                             "candidates (MLflow disabled)",
                             name, kind, len(shadow_dict))
                    continue

                try:
                    _log_shadow_run(
                        exp_id, getattr(ctx, "today", datetime.date.today()),
                        name, kind, primary_kind,
                        primary_scores, shadow_dict,
                        primary_ranks, shadow_ranks,
                    )
                    log.info("ApplyShadowScoringTask: shadow %s (%s) logged %d "
                             "candidates via MLflow", name, kind, len(shadow_dict))
                except Exception as exc:
                    log.warning("ApplyShadowScoringTask: MLflow log failed for %s: %s",
                                 name, exc)
            finally:
                # ALWAYS collect exactly one health record per configured shadow,
                # whichever soft-fail branch above was taken (continue runs the
                # finally). _flush() below finalizes + persists them.
                health_records.append(health)

        # Persist the per-run health records to the append-only JSONL sink so a
        # downstream orchestrator sentinel can catch a silently-degraded shadow.
        # Best-effort + only when a sink is defined (see _flush).
        if health_enabled:
            _flush(health_records)
        return None


__all__ = [
    "ApplyShadowScoringTask",
    # Canonical shadow-health contract (home: shadow_health), re-exported for
    # back-compat. New consumers should import from
    # renquant_pipeline.kernel.panel_pipeline.shadow_health directly.
    "SHADOW_HEALTH_SCHEMA",
    "DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS",
    "DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC",
    "content_digest",
    "resolve_artifact_identity",
    "new_shadow_health",
    "mark_expected_skip",
    "finalize_shadow_health",
    "append_shadow_health",
    "shadow_health_log_path",
    "shadow_health_cfg",
    "shadow_health_sink_defined",
]
