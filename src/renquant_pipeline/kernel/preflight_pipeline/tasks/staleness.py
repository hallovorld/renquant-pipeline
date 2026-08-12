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
        # 2026-08-11: a "blend" is a z-composite of >=2 legs (blend_scorer /
        # pipeline#260, GOAL-8 S1). Its freshness is NOT the single top-level
        # artifact_path but the STALEST of its component legs — a blend is only
        # as fresh as its oldest leg. Assessed via a dedicated branch so the
        # LIVE prod z-blend scorer is actually covered by this rail; before this
        # it took the unrecognised-kind else below and the prod scorer's decay
        # rail was never established (the daily model_freshness monitor reported
        # "binding data cutoff unknown / kind not registered"). SOFT, like the
        # rest — the WF gate stays the promotion/demotion authority.
        if kind == "blend":
            return self._check_blend(ctx, panel_cfg)
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

    # ── kind == "blend": a z-composite is only as fresh as its OLDEST leg ────
    def _check_blend(self, ctx: PreflightContext, panel_cfg: dict) -> PreflightCheck:
        """Assess a blend's freshness as the STALEST of its scoring legs.

        Each leg is resolved the SAME way the per-kind branches resolve a solo
        scorer — REUSING their reads, never reimplementing them: a direct-
        artifact leg (``blend_scorer.PANEL_COMPONENT_KIND`` / ``"panel"``, the
        default when a component omits ``kind``, or ``"xgb"`` /
        ``"panel_ltr_xgboost"``) is read from the artifact JSON exactly like the
        xgb branch; an ``"hf_patchtst"`` leg is read from the sequence sidecar
        exactly like the patchtst branch. The blend's freshness then BINDS to
        the stalest (oldest cutoff / max age) leg, and the identical decay-curve
        / provenance rails the xgb branch applies are applied to that binding
        leg, with every leg's age carried in ``details``.

        Fail-closed provenance discipline (matching the rail's existing
        behaviour): a leg whose kind this rail does not register (e.g.
        ``blend_scorer.MOMENTUM_COMPONENT_KIND`` ``"momentum_residual"``, an
        append-only ledger axis not yet registered here), whose artifact is
        unreadable, or whose ``trained_date`` cannot be established, is a
        SURFACED provenance gap NAMING the leg — never a false "fresh" pass. An
        unestablished leg is the binding constraint (a blend cannot be fresher
        than a leg whose age is unknown). SOFT throughout.
        """
        try:  # canonical component-kind constant (frozen); literal fallback
            from renquant_pipeline.kernel.panel_pipeline.blend_scorer import (  # noqa: PLC0415
                PANEL_COMPONENT_KIND,
            )
        except Exception:  # noqa: BLE001
            PANEL_COMPONENT_KIND = "panel"

        comps = panel_cfg.get("components")
        if not isinstance(comps, list) or not comps:
            return PreflightCheck(
                self.check_name, "soft", False,
                "kind='blend' but ranking.panel_scoring.components is missing "
                "or empty — the blend's legs (and therefore its freshness) "
                "cannot be established (provenance gap, NOT a pass)")

        today = ctx.as_of if getattr(ctx, "as_of", None) else dt.date.today()
        st_cfg = (ctx.config.get("preflight", {}) or {}).get("staleness", {})
        max_retrain = int(st_cfg.get("max_retrain_age_days",
                                     DEFAULT_MAX_RETRAIN_AGE_DAYS))
        max_cutoff = int(st_cfg.get("max_cutoff_age_days",
                                    DEFAULT_MAX_CUTOFF_AGE_DAYS))

        legs = [self._read_blend_leg(ctx, i, entry, today, PANEL_COMPONENT_KIND)
                for i, entry in enumerate(comps)]
        details = {
            "legs": [{k: v for k, v in leg.items() if k != "gap"} for leg in legs],
            "max_retrain_age_days": max_retrain,
            "max_cutoff_age_days": max_cutoff,
        }
        gaps = [leg["gap"] for leg in legs if leg["gap"] is not None]

        # Fail-closed: any leg whose age/axis could not be established BINDS the
        # blend (it is the least-fresh, unbounded leg). Surface and name it, but
        # still report every leg age we DID read so the finding is actionable.
        if gaps:
            readable = [f"component[{leg['index']}] ({leg['kind']}) "
                        f"retrain_age={leg['retrain_age_days']}d"
                        for leg in legs if leg["retrain_age_days"] is not None]
            tail = ("; readable legs: " + ", ".join(readable)) if readable else ""
            return PreflightCheck(
                self.check_name, "soft", False,
                "blend freshness binds to its STALEST leg; unresolved leg(s): "
                + "; ".join(gaps) + tail + " — a blend is only as fresh as its "
                "oldest leg, so an unestablished leg is the binding constraint "
                "(NOT a pass). Register/repair the named leg to make this "
                "actionable.", details=details)

        # Every leg resolved a trained_date. Bind to the stalest (max-age) leg
        # and apply the identical rails the xgb branch applies.
        retrain_leg = max(legs, key=lambda leg: leg["retrain_age_days"])
        details["binding_retrain_leg"] = retrain_leg["index"]
        breaches = []
        if retrain_leg["retrain_age_days"] > max_retrain:
            breaches.append(
                f"binding leg component[{retrain_leg['index']}] "
                f"({retrain_leg['kind']}) retrain age "
                f"{retrain_leg['retrain_age_days']}d > {max_retrain}d "
                f"(quarterly rail)")
        # Decay rail: the blend is bounded by its OLDEST cutoff; a single
        # unstamped leg makes the rail unmeasurable — the SAME surfaced gap the
        # xgb branch keeps (xgb usually does not stamp effective cutoff).
        unstamped = [leg for leg in legs if leg["cutoff_age_days"] is None]
        if unstamped:
            names = ", ".join(f"component[{leg['index']}] ({leg['kind']})"
                              for leg in unstamped)
            breaches.append(
                f"effective_train_cutoff_date unstamped on {names} — "
                "decay-curve rail unmeasurable (provenance gap)")
            details["binding_cutoff_leg"] = None
            cutoff_leg = None
        else:
            cutoff_leg = max(legs, key=lambda leg: leg["cutoff_age_days"])
            details["binding_cutoff_leg"] = cutoff_leg["index"]
            if cutoff_leg["cutoff_age_days"] > max_cutoff:
                breaches.append(
                    f"binding leg component[{cutoff_leg['index']}] "
                    f"({cutoff_leg['kind']}) train-cutoff age "
                    f"{cutoff_leg['cutoff_age_days']}d > {max_cutoff}d "
                    f"(decay-curve knee; measured −0.058 IC by 18mo)")
        if breaches:
            return PreflightCheck(
                self.check_name, "soft", False,
                "blend model staleness: " + "; ".join(breaches) + " — schedule "
                "a retrain through the WF gate", details=details)
        return PreflightCheck(
            self.check_name, "soft", True,
            f"blend fresh: binding leg component[{retrain_leg['index']}] "
            f"({retrain_leg['kind']}) retrained "
            f"{retrain_leg['retrain_age_days']}d ago, oldest cutoff "
            f"{cutoff_leg['cutoff_age_days']}d old across {len(legs)} legs",
            details=details)

    def _read_blend_leg(self, ctx: PreflightContext, index: int, entry,
                        today: dt.date, default_kind: str) -> dict:
        """Resolve ONE blend leg's freshness axis, REUSING the per-kind reads.

        Returns a detail dict; ``gap`` is a human-readable string when the
        leg's provenance could NOT be established (else ``None``). A gap is a
        finding, not a skip — the caller surfaces it fail-closed.
        """
        leg = {"index": index, "kind": None, "artifact": None,
               "trained_date": None, "effective_train_cutoff_date": None,
               "retrain_age_days": None, "cutoff_age_days": None, "gap": None}
        if not isinstance(entry, dict):
            leg["gap"] = f"component[{index}] is not a mapping ({entry!r})"
            return leg
        comp_kind = str(entry.get("kind") or default_kind)
        leg["kind"] = comp_kind
        rel = entry.get("artifact_path")
        if not rel:
            leg["gap"] = f"component[{index}] ({comp_kind}) artifact_path missing"
            return leg
        path = _resolve_artifact_path(ctx.strategy_dir, rel)
        leg["artifact"] = path.name
        try:
            if comp_kind == "hf_patchtst":
                meta, _src = _load_sequence_sidecar(path)  # same read as patchtst
            elif comp_kind in (default_kind, "xgb", "panel_ltr_xgboost"):
                import json  # noqa: PLC0415
                meta = json.loads(path.read_text(encoding="utf-8"))  # same as xgb
            else:
                # Inverted default (never enumerate-and-fall-through): a leg kind
                # this rail does not register — e.g. momentum_residual, whose
                # append-only ledger axis is not read here — is a surfaced gap,
                # not a silent pass. Register it to make this actionable.
                leg["gap"] = (
                    f"component[{index}] kind={comp_kind!r} is not a "
                    f"staleness-readable leg kind — this rail cannot establish "
                    f"its freshness axis (register the kind)")
                return leg
        except Exception as exc:  # noqa: BLE001
            leg["gap"] = (f"component[{index}] ({comp_kind}) artifact dates "
                          f"unreadable for {path.name}: {exc}")
            return leg
        trained = _parse_date(meta.get("trained_date"))
        cutoff = _parse_date(meta.get("effective_train_cutoff_date"))
        if trained is None:
            leg["gap"] = (f"component[{index}] ({comp_kind}, {path.name}) lacks "
                          f"trained_date — provenance gap, leg age unmeasurable")
            return leg
        leg["trained_date"] = trained.isoformat()
        leg["retrain_age_days"] = (today - trained).days
        if cutoff is not None:
            leg["effective_train_cutoff_date"] = cutoff.isoformat()
            leg["cutoff_age_days"] = (today - cutoff).days
        return leg
