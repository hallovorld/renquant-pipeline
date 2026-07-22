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
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from renquant_pipeline.kernel.pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.pipeline import Job, Task

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

# ── Shadow-scorer HEALTH RECORD (silent-failure sentinel feed) ─────────────────
# The shadow scorer is fail-soft by design: a broken artifact_path (e.g. a
# stray ``../../``) makes it load-fail and CONTINUE, so a G4-critical shadow
# data feed can die for weeks with nothing but a per-run log.warning. This
# emits ONE structured, machine-readable health record per configured shadow
# model per run to an append-only JSONL sink so a downstream orchestrator
# sentinel can alarm on silent degradation (unresolved artifact, stale train
# cutoff, low coverage, missing provenance) WITHOUT the shadow ever becoming
# fatal to the live decision pipeline.
#
# SINK (documented for the orchestrator sentinel):
#   <config["_strategy_dir"]>/logs/shadow_scorer_health.jsonl
#   override via config["shadow_health"]["path"]; one JSON object per line,
#   schema tag "shadow_scorer_health.v1".
SHADOW_HEALTH_SCHEMA = "shadow_scorer_health.v1"
DEFAULT_SHADOW_HEALTH_RELPATH = Path("logs") / "shadow_scorer_health.jsonl"
# Freshness bar mirrors the model-freshness governance policy ("NO model
# > 28 days"); coverage bar mirrors the fundamentals min_coverage (0.80) used
# by DataAvailabilityTask. Both operator-overridable under config.shadow_health.
DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS = 28
DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC = 0.80
# Provenance stamps whose absence marks a shadow NOT ACTIONABLE (a comparison
# with no known training cutoff / config fingerprint is unverifiable).
_SHADOW_TRAIN_CUTOFF_FIELD = "effective_train_cutoff_date"
_SHADOW_CONFIG_FP_FIELD = "config_fingerprint"


def _shadow_health_cfg(config: dict) -> dict:
    raw = (config or {}).get("shadow_health")
    return raw if isinstance(raw, dict) else {}


def shadow_health_log_path(config: dict) -> Path:
    """Resolve the append-only JSONL sink for shadow-scorer health records.

    Default: ``<config["_strategy_dir"]>/logs/shadow_scorer_health.jsonl``
    (mirrors the AdmissionShadowLoggerTask sink convention). Overridable via
    ``config["shadow_health"]["path"]``. Falls back to ``./logs/...`` when no
    strategy_dir is set (sim/test)."""
    override = _shadow_health_cfg(config).get("path")
    if override:
        return Path(str(override))
    strategy_dir = (config or {}).get("_strategy_dir")
    base = Path(str(strategy_dir)) if strategy_dir else Path(".")
    return base / DEFAULT_SHADOW_HEALTH_RELPATH


def _parse_cutoff_date(value: Any) -> datetime.date | None:
    """Parse a ``YYYY-MM-DD`` cutoff stamp (leading 10 chars). None if
    absent/unparseable — mirrors job_universe._axis_cutoff parsing."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _new_shadow_health(
    *, shadow_name: str, kind: Any, artifact_path: Any,
    run_date: datetime.date, run_id: Any, n_candidates: int,
) -> dict[str, Any]:
    """A health record pre-seeded to the WORST case (nothing loaded/scored).

    Every field is filled progressively as the per-model score attempt makes
    progress; finalize_shadow_health() then derives ``actionable``/``reasons``.
    Pre-seeding to the worst case means an early ``continue`` still emits a
    correct, self-describing record."""
    return {
        "schema": SHADOW_HEALTH_SCHEMA,
        "run_date": run_date.isoformat(),
        "run_id": str(run_id) if run_id is not None else None,
        "shadow_name": shadow_name,
        "kind": kind,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "artifact_resolved": False,
        "artifact_resolved_path": None,
        "loaded": False,
        "load_error": None,
        _SHADOW_TRAIN_CUTOFF_FIELD: None,
        "staleness_days": None,
        _SHADOW_CONFIG_FP_FIELD: None,
        "n_candidates": int(n_candidates),
        "n_scored": 0,
        "coverage_frac": None,
        "skip_reason": None,
        "actionable": False,
        "reasons": [],
    }


def finalize_shadow_health(
    health: dict[str, Any], *, run_date: datetime.date,
    max_staleness_days: int = DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    min_coverage_frac: float = DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
) -> dict[str, Any]:
    """Derive the ``actionable`` verdict + machine-readable ``reasons`` list.

    A shadow is ACTIONABLE only if it loaded, produced a fresh-enough training
    cutoff, carries provenance (config fingerprint), and scored a high-enough
    fraction of the candidate cross-section. Any failing dimension appends a
    stable reason token so a sentinel can classify the degradation. Pure /
    side-effect-free so it is directly unit-testable."""
    reasons: list[str] = []
    if not health.get("loaded"):
        health["staleness_days"] = None
        health["actionable"] = False
        health["reasons"] = [
            "artifact_unresolved" if not health.get("artifact_resolved")
            else "load_failed"
        ]
        return health

    # Training-cutoff freshness (the stale-shadow class).
    cutoff_raw = health.get(_SHADOW_TRAIN_CUTOFF_FIELD)
    cutoff = _parse_cutoff_date(cutoff_raw)
    if cutoff_raw in (None, ""):
        health["staleness_days"] = None
        reasons.append("missing_train_cutoff")
    elif cutoff is None:
        health["staleness_days"] = None
        reasons.append("unparseable_train_cutoff")
    else:
        staleness = (run_date - cutoff).days
        health["staleness_days"] = staleness
        if staleness < 0:
            reasons.append(f"train_cutoff_future_{staleness}d")
        elif staleness > max_staleness_days:
            reasons.append(f"stale_{staleness}d_limit_{max_staleness_days}d")

    # Provenance: an unfingerprinted comparison is unverifiable.
    if not health.get(_SHADOW_CONFIG_FP_FIELD):
        reasons.append("missing_config_fingerprint")

    # Coverage of the candidate cross-section.
    if health.get("n_scored", 0) <= 0:
        reasons.append(health.get("skip_reason") or "no_scores")
    else:
        cov = health.get("coverage_frac")
        if cov is not None and cov < min_coverage_frac:
            reasons.append(
                f"low_coverage_{cov:.2f}_min_{min_coverage_frac:.2f}")

    health["actionable"] = not reasons
    health["reasons"] = reasons
    return health


def append_shadow_health(path: str | Path, record: dict[str, Any]) -> None:
    """Append one health record as a JSON line to ``path`` (creates dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str)
    with p.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _resolve_shadow_artifact_path(
    artifact_path: str | Path,
    *,
    strategy_dir: str | Path | None,
    repo: Path | None = None,
) -> Path:
    p = Path(artifact_path)
    if p.is_absolute():
        return p

    # 2026-06-11 shadow-dead fix: resolve like the PRIMARY scorer —
    # strategy_dir first, repo data_root as back-compat fallback. Pre-fix this
    # resolved only against data_root(), so the post-PatchTST-promotion shadow
    # under <strategy_dir>/artifacts/prod failed to load on every run.
    if strategy_dir:
        sd = Path(strategy_dir) / p
        if sd.exists():
            return sd
    # 2026-06-27: resolve data_root() LAZILY — only when the strategy_dir
    # candidate misses and we actually need the umbrella repo fallback. A fully
    # stubbed/cached call path (unit tests) then never requires a data root.
    if repo is None:
        from renquant_pipeline.kernel.panel_pipeline._data_root import data_root  # noqa: PLC0415
        repo = data_root()
    return repo / p


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
        if panel_cfg.get("shadow_enabled", True) is False:
            return None
        shadow_models = panel_cfg.get("shadow_models", []) or []
        if not shadow_models:
            return None

        # Primary scores (must be set by main ApplyScoresTask)
        cands = list(ctx.candidates) if ctx.candidates else []
        if not cands:
            log.info("ApplyShadowScoringTask: 0 candidates — skip")
            return None
        primary_scores = {c.ticker: float(c.panel_score)
                          for c in cands if c.panel_score is not None}
        if not primary_scores:
            return None
        sorted_primary = sorted(primary_scores.items(), key=lambda x: -x[1])
        primary_ranks = {t: i + 1 for i, (t, _) in enumerate(sorted_primary)}
        primary_kind = panel_cfg.get("kind", "xgb")

        shadow_log_mlflow = bool(panel_cfg.get("shadow_log_mlflow", True))
        exp_id = None
        if shadow_log_mlflow:
            try:
                exp_id = _ensure_mlflow_setup(
                    panel_cfg.get("shadow_tracking_uri"),
                    panel_cfg.get("shadow_experiment"))
            except Exception as exc:
                log.warning("ApplyShadowScoringTask: MLflow setup failed: %s — skip",
                             exc)
                return None

        from renquant_pipeline.kernel.panel_pipeline.model_registry import registry  # noqa: PLC0415
        # data_root() is resolved lazily inside _resolve_shadow_artifact_path —
        # only when a path actually needs the umbrella repo fallback — so a fully
        # stubbed/cached call path (e.g. unit tests) never requires a data root.

        # ── Shadow HEALTH RECORD wiring (silent-failure sentinel feed) ──────
        # Normalize the session date once (ctx.today may be a pd.Timestamp /
        # datetime / date). Read operator thresholds. health_records accrues
        # ONE record per configured shadow model regardless of which soft-fail
        # branch it takes — the per-model try/finally below guarantees it.
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
        hcfg = _shadow_health_cfg(ctx.config)
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
        health_records: list[dict[str, Any]] = []

        for sm in shadow_models:
            name = sm.get("name", "unnamed_shadow")
            kind = sm.get("kind")
            artifact_path = sm.get("artifact_path")
            health = _new_shadow_health(
                shadow_name=name, kind=kind, artifact_path=artifact_path,
                run_date=run_date, run_id=run_id,
                n_candidates=len(primary_scores),
            )
            try:
                if not kind or not artifact_path:
                    health["load_error"] = "missing kind/artifact_path"
                    log.warning("ApplyShadowScoringTask: shadow %s missing "
                                 "kind/artifact_path", name)
                    continue
                p = _resolve_shadow_artifact_path(
                    artifact_path,
                    strategy_dir=ctx.config.get("_strategy_dir"),
                )
                # Load-time artifact-resolution check (requirement 3): a broken
                # relative artifact_path (the ``../../`` class) resolves to a
                # nonexistent file → artifact_resolved=False and the load_error
                # names the offending path. Recorded here so a cached scorer is
                # also stamped resolved; the loader still runs (some artifacts
                # are directories) — non-existence just pre-labels the error.
                health["artifact_resolved_path"] = str(p)
                health["artifact_resolved"] = p.exists()
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
                        if not health["artifact_resolved"]:
                            health["load_error"] = (
                                f"artifact_path {artifact_path!r} does not "
                                f"resolve to an existing file (resolved: {p}): "
                                f"{exc}")
                        else:
                            health["load_error"] = str(exc)
                        log.warning("ApplyShadowScoringTask: shadow %s (%s) load failed: %s",
                                     name, kind, exc)
                        continue
                    _SCORER_CACHE[cache_key] = scorer

                # Scorer available — the shadow LOADED. Stamp provenance from
                # its metadata so a stale/unfingerprinted shadow is visible.
                health["loaded"] = True
                _meta = getattr(scorer, "metadata", {}) or {}
                if isinstance(_meta, dict):
                    health[_SHADOW_TRAIN_CUTOFF_FIELD] = _meta.get(
                        _SHADOW_TRAIN_CUTOFF_FIELD)
                    health[_SHADOW_CONFIG_FP_FIELD] = _meta.get(
                        _SHADOW_CONFIG_FP_FIELD)

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
                # ALWAYS emit exactly one health record per configured shadow,
                # whichever soft-fail branch above was taken (continue runs the
                # finally). This is the silent-failure sentinel feed — it MUST
                # NOT itself raise, so finalize is defensively wrapped.
                try:
                    finalize_shadow_health(
                        health, run_date=run_date,
                        max_staleness_days=max_staleness_days,
                        min_coverage_frac=min_coverage_frac)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "ApplyShadowScoringTask: health finalize failed for %s",
                        name)
                health_records.append(health)

        # Persist the per-run health records to the append-only JSONL sink so a
        # downstream orchestrator sentinel can catch a silently-degraded shadow
        # (unresolved artifact / stale cutoff / low coverage). Best-effort:
        # NEVER fail the (already non-fatal) shadow task on a health write.
        # Only persist when a sink location is DEFINED — the sentinel feed lives
        # under <strategy_dir>/logs, so with no strategy_dir and no explicit
        # override we skip the write rather than scatter the file in a bare cwd.
        sink_defined = bool(hcfg.get("path")) or bool(
            ctx.config.get("_strategy_dir"))
        if health_records and not sink_defined:
            log.debug(
                "ApplyShadowScoringTask: %d shadow health record(s) not "
                "persisted (no _strategy_dir / shadow_health.path configured)",
                len(health_records))
        elif health_records:
            try:
                sink = shadow_health_log_path(ctx.config)
                for rec in health_records:
                    append_shadow_health(sink, rec)
                n_actionable = sum(1 for r in health_records
                                   if r.get("actionable"))
                log.info(
                    "ApplyShadowScoringTask: wrote %d shadow health record(s) "
                    "to %s (%d actionable)",
                    len(health_records), sink, n_actionable)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "ApplyShadowScoringTask: shadow health write failed: %s", exc)

        return None


__all__ = [
    "ApplyShadowScoringTask",
    "SHADOW_HEALTH_SCHEMA",
    "DEFAULT_SHADOW_HEALTH_RELPATH",
    "DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS",
    "DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC",
    "shadow_health_log_path",
    "finalize_shadow_health",
    "append_shadow_health",
]
