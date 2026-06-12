"""P-MODEL-STALENESS — active model age vs the retrain rails.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §0.5 /
§6 ("quarterly freshness rail, measured 6-7 IC pts") and the 2026-06
three-point staleness decay curve (within-pipeline: −0.005 / −0.058 /
−0.070 IC at 11 / 18 / 24 months of train-cutoff age) — decay is
monotone and accelerates after ~12 months.

Two independent ages, two knobs (preflight.staleness, both warn-only):

  * retrain age   — days since the artifact was TRAINED
    (``trained_date``); rail: quarterly retrain cadence.
    ``max_retrain_age_days``, default 120.
  * cutoff age    — days since the last TRAINING DATA the model saw
    (``effective_train_cutoff_date``); rail: the decay curve.
    ``max_cutoff_age_days``, default 335 (~11 months — the knee).

SOFT severity: staleness is a degradation signal, not corruption — the
WF gate remains the promotion/demotion authority. Missing sidecar dates
are themselves a SOFT finding (provenance gap), never a pass.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.preflight import (  # noqa: PLC0415 (legacy bridge)
    PreflightCheck,
    _load_sequence_sidecar,
    _resolve_artifact_path,
)

from ..base import PreflightTask
from ..ctx import PreflightContext

DEFAULT_MAX_RETRAIN_AGE_DAYS = 120
DEFAULT_MAX_CUTOFF_AGE_DAYS = 335


def _parse_date(raw) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


class ModelStalenessTask(PreflightTask):
    """P-MODEL-STALENESS — warn when the active scorer outlives its rails."""

    check_name = "P-MODEL-STALENESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        panel_cfg = (ctx.config.get("ranking", {}) or {}).get("panel_scoring", {})
        if not panel_cfg.get("enabled", False):
            return PreflightCheck(self.check_name, "soft", True,
                                  "panel scoring disabled — skip")
        if str(panel_cfg.get("kind", "xgb")) != "hf_patchtst":
            return PreflightCheck(
                self.check_name, "soft", True,
                f"kind={panel_cfg.get('kind')!r} has no sequence sidecar "
                f"dates yet — skip (extend when XGB stamps trained_date)")
        rel = panel_cfg.get("artifact_path")
        if not rel:
            return PreflightCheck(self.check_name, "soft", False,
                                  "panel_scoring.artifact_path missing")
        path = _resolve_artifact_path(ctx.strategy_dir, rel)
        try:
            sidecar, source = _load_sequence_sidecar(path)
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", False,
                f"sidecar unreadable for {path.name}: {exc}")

        trained = _parse_date(sidecar.get("trained_date"))
        cutoff = _parse_date(sidecar.get("effective_train_cutoff_date"))
        if trained is None or cutoff is None:
            return PreflightCheck(
                self.check_name, "soft", False,
                f"sidecar {source.name} lacks trained_date/"
                f"effective_train_cutoff_date — provenance gap, staleness "
                f"unmeasurable (NOT a pass)")

        st_cfg = (ctx.config.get("preflight", {}) or {}).get("staleness", {})
        max_retrain = int(st_cfg.get("max_retrain_age_days",
                                     DEFAULT_MAX_RETRAIN_AGE_DAYS))
        max_cutoff = int(st_cfg.get("max_cutoff_age_days",
                                    DEFAULT_MAX_CUTOFF_AGE_DAYS))
        today = dt.date.today()
        retrain_age = (today - trained).days
        cutoff_age = (today - cutoff).days
        details = {"trained_date": trained.isoformat(),
                   "effective_train_cutoff_date": cutoff.isoformat(),
                   "retrain_age_days": retrain_age,
                   "cutoff_age_days": cutoff_age,
                   "max_retrain_age_days": max_retrain,
                   "max_cutoff_age_days": max_cutoff}
        breaches = []
        if retrain_age > max_retrain:
            breaches.append(
                f"retrain age {retrain_age}d > {max_retrain}d (quarterly rail)")
        if cutoff_age > max_cutoff:
            breaches.append(
                f"train-cutoff age {cutoff_age}d > {max_cutoff}d (decay-curve "
                f"knee; measured −0.058 IC by 18mo)")
        if breaches:
            return PreflightCheck(
                self.check_name, "soft", False,
                "model staleness: " + "; ".join(breaches) + " — schedule a "
                "retrain through the WF gate", details=details)
        return PreflightCheck(
            self.check_name, "soft", True,
            f"model fresh: retrained {retrain_age}d ago, "
            f"cutoff {cutoff_age}d old", details=details)
