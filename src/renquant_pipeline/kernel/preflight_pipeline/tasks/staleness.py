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
        kind = str(panel_cfg.get("kind", "xgb"))
        rel = panel_cfg.get("artifact_path")
        if not rel:
            return PreflightCheck(self.check_name, "soft", False,
                                  "panel_scoring.artifact_path missing")
        path = _resolve_artifact_path(ctx.strategy_dir, rel)

        # 2026-06-27: read the ACTIVE model's dates regardless of kind so the
        # LIVE primary is actually covered. Previously this check skipped for
        # any non-hf_patchtst kind, so the xgb primary's age was never gated —
        # the staleness rail did nothing for the model actually driving trades.
        # hf_patchtst stamps a sequence sidecar; xgb/panel_ltr_xgboost stamp
        # trained_date on the artifact JSON itself (effective_train_cutoff_date
        # is usually absent for xgb → a provenance gap we SURFACE, not skip).
        try:
            if kind == "hf_patchtst":
                meta, source = _load_sequence_sidecar(path)
                source_name = source.name
            elif kind in ("xgb", "panel_ltr_xgboost"):
                import json  # noqa: PLC0415
                meta = json.loads(path.read_text(encoding="utf-8"))
                source_name = path.name
            else:
                # 2026-07-30: an UNRECOGNISED kind is ALWAYS a non-pass.
                #
                # The 06-27 note above fixed this once — for xgb — by ADDING a kind
                # to the allow-list. Enumerating leaves the default fail-OPEN, and
                # the same shape recurred: measured 2026-07-30, the live shadow-BLEND
                # lane runs `kind='blend'` and its staleness check was skipped
                # entirely while it issued buy recommendations. `patchtst` (without
                # the `hf_` prefix) and an absent kind (`None`) are also present in
                # committed strategy configs and took the same branch.
                #
                # My first fix measured the dates best-effort and PASSED when they
                # were fresh. Review rejected that (#233) and is right: freshness
                # being measurable does not establish that an unrecognised artifact
                # carries the schema or training provenance this rail requires.
                # Passing on fresh dates SILENTLY CERTIFIES A NEW MODEL KIND, which
                # is exactly the extension work that has to stay visible.
                #
                # So: never a pass. But the measured freshness is REPORTED in the
                # message, because discarding it would make the finding
                # unactionable — the reader needs to know whether registering this
                # kind is routine or urgent. This check is SOFT, so a non-pass
                # surfaces without blocking the run.
                measured = "dates unreadable"
                try:
                    import json  # noqa: PLC0415
                    probe = json.loads(path.read_text(encoding="utf-8"))
                    t = _parse_date(probe.get("trained_date"))
                    c = _parse_date(probe.get("effective_train_cutoff_date"))
                    today = ctx.as_of if getattr(ctx, "as_of", None) else dt.date.today()
                    parts = []
                    parts.append(f"trained_date={t.isoformat()} "
                                 f"(age {(today - t).days}d)" if t else
                                 "trained_date absent")
                    parts.append(f"cutoff={c.isoformat()} (age {(today - c).days}d)"
                                 if c else "effective_train_cutoff_date absent")
                    measured = "; ".join(parts)
                except Exception as exc:  # noqa: BLE001
                    measured = f"artifact unreadable: {exc}"
                return PreflightCheck(
                    self.check_name, "soft", False,
                    f"kind={kind!r} is not a registered scoring kind — this rail "
                    f"cannot establish its schema or training provenance, so it is "
                    f"NOT a staleness pass however fresh it looks. Measured anyway: "
                    f"{measured}. Register the kind to make this actionable.")
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", False,
                f"artifact dates unreadable for {path.name}: {exc}")

        trained = _parse_date(meta.get("trained_date"))
        cutoff = _parse_date(meta.get("effective_train_cutoff_date"))
        if trained is None:
            return PreflightCheck(
                self.check_name, "soft", False,
                f"{source_name} lacks trained_date — provenance gap, model "
                f"age unmeasurable (NOT a pass)")

        st_cfg = (ctx.config.get("preflight", {}) or {}).get("staleness", {})
        max_retrain = int(st_cfg.get("max_retrain_age_days",
                                     DEFAULT_MAX_RETRAIN_AGE_DAYS))
        max_cutoff = int(st_cfg.get("max_cutoff_age_days",
                                    DEFAULT_MAX_CUTOFF_AGE_DAYS))
        today = dt.date.today()
        retrain_age = (today - trained).days
        cutoff_age = (today - cutoff).days if cutoff is not None else None
        details = {"trained_date": trained.isoformat(),
                   "effective_train_cutoff_date": (
                       cutoff.isoformat() if cutoff is not None else None),
                   "retrain_age_days": retrain_age,
                   "cutoff_age_days": cutoff_age,
                   "max_retrain_age_days": max_retrain,
                   "max_cutoff_age_days": max_cutoff}
        breaches = []
        if retrain_age > max_retrain:
            breaches.append(
                f"retrain age {retrain_age}d > {max_retrain}d (quarterly rail)")
        if cutoff_age is None:
            breaches.append(
                "effective_train_cutoff_date unstamped — decay-curve rail "
                "unmeasurable (provenance gap; xgb does not stamp it)")
        elif cutoff_age > max_cutoff:
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
