"""Composite BLEND panel scorer — the certified z(prod) + z(clf) construction.

Implements the blend objective certified by the renquant-model#74/75/76
confirmatory line (prereg model#75, results model#76; design reference
pipeline#213 `doc/design/2026-07-25-blend-shadow-deployment.md`) as a
first-class scorer kind, so a shadow profile can run the blend as its
PRIMARY scorer through the FULL decision funnel:

    blend_score = z(prod_score) + z(clf_score)   per scoring call,
                  z cross-sectional (ddof=0) over each component's
                  scored universe at scoring time.

Config schema (frozen; dispatched via ``model_registry`` kind ``blend``)::

    ranking.panel_scoring.kind = "blend"
    ranking.panel_scoring.components = [
      {"artifact_path": "...panel-ltr.alpha158_fund.json",      # production scorer
       "expected_content_sha256":     "sha256:<hex-prefix-or-full>",
       "expected_config_fingerprint": "sha256:<fp>"},
      {"artifact_path": "...panel-clf.top-decile.fwd60.json",   # top-decile clf
       "expected_content_sha256":     "sha256:<hex-prefix-or-full>",
       "expected_config_fingerprint": "sha256:<fp>"},
    ]

Identity pins (BOTH keys REQUIRED per component; verified fail-closed at
load, mirroring the #211 shadow-health digest rules):

* ``expected_content_sha256`` — sha256 of the artifact file bytes.
  Compared via the shadow-health ``_norm_digest`` normalization (strip the
  optional ``sha256:`` prefix, lowercase) and ABBREV-TOLERANT: an
  abbreviated pin matches when it is a prefix (>= 8 hex chars) of the full
  observed digest, so both the 16-hex convention (``resolve_artifact``'s
  run-fingerprint prefix, the existing shadow_models pin format) and a full
  64-hex pin verify.
* ``expected_config_fingerprint`` — the artifact's stored training-config
  fingerprint. Compared VERBATIM, tolerant of both forms (with or without
  the ``sha256:`` prefix on either side); no abbreviation.

Component ``kind`` dispatch (pipeline#260, GOAL-8 S1)::

    {"kind": "momentum_residual",                       # ledger-pointer leg
     "artifact_path": ".../momentum_artifact_ledger.jsonl",
     "expected_config_fingerprint": "momentum-v0-<sha16>"}

* Absent ``kind`` (or ``"panel"``) = the classic direct-artifact leg above —
  byte-identical behavior; the certified z(prod)+z(clf) profile carries no
  ``kind`` keys.
* ``momentum_residual`` loads through ``load_momentum_residual_scorer``
  (single-read snapshot → chain → tail dated artifact → sha both directions
  → row↔artifact parity → golden reproduction). ``expected_content_sha256``
  is REFUSED (append-only ledger = byte pin stale by design; the chain + the
  tail row's artifact sha are the swap anchors) and
  ``expected_config_fingerprint`` pins the RECIPE — the loader-stamped
  ``momentum-<params_version>-<sha256(canonical params)[:16]>``, stable
  across weekly publishes with unchanged frozen params.
* Any other ``kind`` fails closed (inverted default, no fall-through).
* Cross-section semantics UNCHANGED: a leg's unscored names stay NaN and
  NaN propagates through the sum, so the composite scores the INTERSECTION
  of the legs' scored universes. The S1 prereg freezes this semantic
  explicitly before any run.

Failure semantics:

* Either component fails to LOAD (missing file, bad JSON, pin mismatch,
  missing pin, history-requiring artifact) → raise (fail CLOSED — the
  primary LoadScorerTask converts this into ``panel_scorer_load_failed``
  and clears the buy path).
* A component whose score cross-section is DEGENERATE at scoring time
  (std == 0, or fewer than 2 scored names) contributes 0 to the blend and
  ``metadata["degraded_reason"]`` records a reason token — fail SOFT
  within the composite, but visibly.

Composite ``config_fingerprint`` recipe (deterministic): the sha256 hex
digest of the UTF-8 bytes of the two component config fingerprints — their
stored VERBATIM forms — joined by a single ``"\\n"``, in config
``components`` order, prefixed ``"sha256:"``::

    sha256:hexdigest(sha256(fp_component0 + "\\n" + fp_component1))

Component order is therefore identity-bearing: swapping the components
produces a different composite fingerprint (the certified construction
fixes component 0 = production scorer, component 1 = classifier).
"""
from __future__ import annotations

import datetime
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.feature_transform import (
    transform_feature_frame,
)
# ONE digest normalizer (#211) — do not fork a local copy.
from renquant_pipeline.kernel.panel_pipeline.shadow_health import _norm_digest

log = logging.getLogger("kernel.panel_pipeline.blend_scorer")

BLEND_KIND = "blend"
REQUIRED_COMPONENT_KEYS = (
    "artifact_path",
    "expected_content_sha256",
    "expected_config_fingerprint",
)
# pipeline#260 (GOAL-8 S1): per-component kind dispatch. Absent `kind` means
# the classic direct-artifact leg — byte-identical behavior for the certified
# z(prod)+z(clf) profile, whose config carries no `kind` keys. The momentum
# leg loads through the ledger-chain loader instead and pins the RECIPE
# (params fingerprint), never file bytes (append-only ledger = stale by
# design; same refusal the umbrella candidate-pin gate enforces).
PANEL_COMPONENT_KIND = "panel"
MOMENTUM_COMPONENT_KIND = "momentum_residual"
MOMENTUM_COMPONENT_REQUIRED_KEYS = (
    "artifact_path",
    "expected_config_fingerprint",
)
# GOAL-9 (orch#794 AC3, decided 2026-08-04): the combination rule is an
# unweighted sum of per-component cross-sectional z-scores and the scoring
# loop is N-ready; the certified 2-component construction generalizes
# VERBATIM to N >= 2. Per-component weights are deliberately NOT introduced
# here — weighting is the MoE stage's own preregistered change (AC5).
MIN_COMPONENTS = 2
# Below this many hex chars a content pin is rejected outright — a too-short
# prefix stops being an identity claim.
MIN_CONTENT_PIN_HEX = 8


def content_pin_matches(expected: Any, observed: Any) -> bool:
    """Abbrev-tolerant content-digest compare (#211 ``_norm_digest`` rules).

    Both sides are normalized (optional ``sha256:`` prefix stripped,
    lowercased); they match when the shorter is a prefix of the longer and
    the pin carries at least ``MIN_CONTENT_PIN_HEX`` hex chars.
    """
    e = _norm_digest(expected)
    o = _norm_digest(observed)
    if not e or not o:
        return False
    if min(len(e), len(o)) < MIN_CONTENT_PIN_HEX:
        return False
    shorter, longer = (e, o) if len(e) <= len(o) else (o, e)
    return longer.startswith(shorter)


def config_fp_pin_matches(expected: Any, observed: Any) -> bool:
    """Verbatim config-fingerprint compare, tolerant of BOTH written forms
    (``sha256:<fp>`` and bare ``<fp>``) on either side. No abbreviation."""
    if expected is None or observed is None:
        return False
    e = str(expected).strip()
    o = str(observed).strip()
    if not e or not o:
        return False
    e_body = e.split(":", 1)[-1]
    o_body = o.split(":", 1)[-1]
    return bool(e_body) and e_body == o_body


def composite_config_fingerprint(component_fps: list[str]) -> str:
    """Deterministic composite fp: ``"sha256:" + sha256(fp0 + "\\n" + fp1)``.

    ``component_fps`` are the components' STORED fingerprints verbatim, in
    config ``components`` order (order is identity-bearing — see module
    docstring).
    """
    blob = "\n".join(str(fp) for fp in component_fps).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _parse_date(value: Any) -> datetime.date | None:
    """Parse a ``YYYY-MM-DD`` stamp (leading 10 chars) — mirrors
    ``shadow_health._parse_cutoff_date`` semantics."""
    if value is None:
        return None
    s = str(value).strip()[:10]
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class BlendComponent:
    """One verified leg of the composite: the loaded scorer + its identity."""

    scorer: Any
    artifact_path: str          # resolved absolute path actually loaded
    content_sha256: str         # "sha256:" + FULL 64-hex digest of file bytes
    config_fingerprint: str     # stored form, verbatim
    trained_date: str | None
    effective_train_cutoff_date: str | None

    def identity(self) -> dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "content_sha256": self.content_sha256,
            "config_fingerprint": self.config_fingerprint,
            "trained_date": self.trained_date,
            "effective_train_cutoff_date": self.effective_train_cutoff_date,
        }


class BlendPanelScorer:
    """Composite scorer: sum of per-component cross-sectional z-scores.

    Exposes the same scorer interface as ``PanelScorer`` — ``feature_cols``
    (sorted union of the components'), ``requires_history`` (False),
    ``score(feature_matrix, ctx=None) -> pd.Series`` — so it slots into
    every ``PanelScorer`` call site (LoadScorerTask / ApplyScoresTask /
    shadow scoring) unchanged.

    INPUT SPACE CONTRACT: ``score`` expects the union feature matrix in RAW
    feature space. Each component's stored raw→model transform
    (``transform_feature_frame`` with THAT component's
    ``feature_means``/``feature_stds``) is applied internally per leg —
    the components carry DIFFERENT normalization stats, so a single outer
    transform cannot be correct for both. ``ApplyScoresTask`` therefore
    skips its outer transform for kind ``blend``.

    ``metadata`` carries: ``kind="blend"``, both components' identities,
    ``effective_train_cutoff_date`` = the OLDER of the two components
    (conservative staleness; ``None`` when either leg is unstamped —
    surfaced, not hidden), and the deterministic composite
    ``config_fingerprint`` (recipe in the module docstring).
    """

    requires_history = False
    seq_len = 1

    def __init__(self, components: list[BlendComponent]):
        if len(components) < MIN_COMPONENTS:
            raise ValueError(
                f"BlendPanelScorer needs at least {MIN_COMPONENTS} components, "
                f"got {len(components)}")
        self.components = list(components)
        feat: set[str] = set()
        for comp in self.components:
            feat.update(getattr(comp.scorer, "feature_cols", []))
        self.feature_cols = sorted(feat)
        cutoffs = [
            _parse_date(c.effective_train_cutoff_date) for c in self.components
        ]
        effective = (
            min(cutoffs).isoformat() if all(c is not None for c in cutoffs)
            else None
        )
        self.metadata: dict[str, Any] = {
            "kind": BLEND_KIND,
            "components": [c.identity() for c in self.components],
            "effective_train_cutoff_date": effective,
            "config_fingerprint": composite_config_fingerprint(
                [c.config_fingerprint for c in self.components]),
            "degraded_reason": None,
        }

    def score(self, feature_matrix: pd.DataFrame, ctx: Any = None) -> pd.Series:
        """blend = Σ_components z(component_score) over ``feature_matrix``.

        ``ctx`` is accepted-but-ignored for signature uniformity with
        ``PanelScorer`` / ensemble variants. Missing union columns raise
        ``KeyError`` (same contract as ``PanelScorer.score`` — the caller
        aligns the matrix to ``feature_cols``). z uses ddof=0 over each
        component's finite-scored universe; a degenerate leg (std == 0 or
        < 2 scored names) contributes 0 and is recorded in
        ``metadata["degraded_reason"]``. A name a healthy leg could not
        score finitely gets NaN (dropped downstream as unscored).
        """
        del ctx  # regime-blind, like PanelScorer
        missing = [c for c in self.feature_cols if c not in feature_matrix.columns]
        if missing:
            raise KeyError(
                f"BlendPanelScorer.score: feature matrix missing columns: {missing}",
            )
        n_rows = len(feature_matrix)
        total = np.zeros(n_rows, dtype=float)
        reasons: list[str] = []
        for i, comp in enumerate(self.components):
            comp_cols = list(getattr(comp.scorer, "feature_cols", []))
            comp_meta = getattr(comp.scorer, "metadata", {}) or {}
            # Per-leg raw→model transform: identical math to the primary
            # ApplyScoresTask path for a solo panel_ltr_xgboost artifact.
            x_c = transform_feature_frame(
                feature_matrix, comp_cols, comp_meta, source_space="raw",
            )
            raw = comp.scorer.score(x_c)
            vals = pd.Series(raw).reindex(feature_matrix.index).to_numpy(dtype=float)
            finite = np.isfinite(vals)
            n_finite = int(finite.sum())
            label = Path(comp.artifact_path).stem
            if n_finite < 2:
                reasons.append(f"component{i}[{label}]_n_lt_2")
                log.warning(
                    "BlendPanelScorer: component %d (%s) scored %d name(s) — "
                    "needs >=2 for a cross-sectional z; contributing 0.",
                    i, label, n_finite)
                continue
            mu = float(vals[finite].mean())
            sd = float(vals[finite].std())  # ddof=0 (numpy default)
            if not np.isfinite(sd) or sd <= 0.0:
                reasons.append(f"component{i}[{label}]_std_zero")
                log.warning(
                    "BlendPanelScorer: component %d (%s) score std=%s over "
                    "%d names — degenerate; contributing 0.",
                    i, label, sd, n_finite)
                continue
            z = np.full(n_rows, np.nan, dtype=float)
            z[finite] = (vals[finite] - mu) / sd
            total = total + z  # NaN propagates for unscored names
        self.metadata["degraded_reason"] = reasons or None
        out = pd.Series(total, index=feature_matrix.index, name="panel_score")
        # Same output soft-contract as PanelScorer (log-only degeneracy check).
        from renquant_pipeline.kernel.panel_pipeline.model_contract import (  # noqa: PLC0415
            soft_check_score_series,
        )
        soft_check_score_series(out, model_name="BlendPanelScorer")
        return out


def _resolve_component_path(ref: str, strategy_dir: Any) -> Path:
    """Resolve a component ref through the ONE resolution authority
    (``kernel.artifact_resolver.resolve_artifact``) — fail-closed."""
    from renquant_pipeline.kernel.artifact_resolver import resolve_artifact  # noqa: PLC0415

    resolved = resolve_artifact(
        ref, strategy_dir=Path(str(strategy_dir)) if strategy_dir else Path("."),
    )
    return resolved.path


def _load_momentum_component(
    i: int, entry: dict, strategy_dir: Any
) -> BlendComponent:
    """Load + pin-verify a ``kind: momentum_residual`` component.

    The heavy verification (single-read ledger snapshot, chain, tail dated
    artifact, content sha both directions, row↔artifact parity, golden
    reproduction) is the ONE existing loader —
    ``load_momentum_residual_scorer`` — never reimplemented here.

    Identity contract differs from a classic leg BY DESIGN:

    * ``expected_content_sha256`` is REFUSED: the ledger is append-only and
      changes on every weekly publish, so a byte pin is stale by design (the
      same refusal the umbrella candidate-pin gate enforces on ledger
      pointers). The chain + the tail row's artifact sha are the
      swap-detection anchors.
    * ``expected_config_fingerprint`` is REQUIRED and pins the RECIPE: the
      loader stamps ``momentum-<params_version>-<sha256(canonical params)[:16]>``
      (recomputable from the artifact by any reader), stable across weekly
      publishes with unchanged frozen params.

    A chain-verified EMPTY ledger raises ``ShadowNotYetPublished`` from the
    inner loader; for a blend PRIMARY that is fail-closed (the composite
    cannot exist), matching the panel_scorer_load_failed funnel semantics.
    """
    from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (  # noqa: PLC0415
        load_momentum_residual_scorer,
    )

    if entry.get("expected_content_sha256"):
        raise ValueError(
            f"blend component[{i}] (momentum_residual) must not carry "
            "expected_content_sha256 — the append-only ledger changes on "
            "every weekly publish, so a byte pin is stale by design; pin "
            "expected_config_fingerprint (the params fingerprint) instead")
    for key in MOMENTUM_COMPONENT_REQUIRED_KEYS:
        if not entry.get(key):
            raise ValueError(
                f"blend component[{i}] (momentum_residual) missing required "
                f"key {key!r} — the recipe pin is mandatory (fail-closed)")
    path = _resolve_component_path(str(entry["artifact_path"]), strategy_dir)
    scorer = load_momentum_residual_scorer(path)
    meta = getattr(scorer, "metadata", {}) or {}
    observed_fp = meta.get("config_fingerprint")
    if not config_fp_pin_matches(
            entry["expected_config_fingerprint"], observed_fp):
        raise ValueError(
            f"blend component[{i}] (momentum_residual) config_fingerprint "
            f"MISMATCH for {path}: "
            f"pinned={entry['expected_config_fingerprint']!r} "
            f"observed={observed_fp!r}")
    log.info(
        "load_blend_scorer: component[%d] momentum_residual verified "
        "(ledger tail row %s, cutoff %s, artifact %s, fp %s)",
        i, meta.get("ledger_row_index"), meta.get("cutoff_date"),
        str(meta.get("artifact_content_sha256"))[:19], observed_fp)
    return BlendComponent(
        scorer=scorer,
        artifact_path=str(path),
        # Observation, not a config pin: the served dated artifact's
        # self-carried identity (pinned by the append-only LEDGER ROW; it
        # legitimately changes every weekly publish).
        content_sha256=str(meta.get("artifact_content_sha256")),
        config_fingerprint=str(observed_fp),
        trained_date=meta.get("trained_date"),
        effective_train_cutoff_date=meta.get("effective_train_cutoff_date"),
    )


def load_blend_scorer(config: dict) -> BlendPanelScorer:
    """Load + pin-verify both components from config; fail closed on ANY gap.

    Raises ``ValueError`` (bad/missing config or pin mismatch) or
    ``FileNotFoundError`` (unresolvable component ref); component artifact
    load errors propagate untouched.
    """
    from renquant_pipeline.kernel.panel_pipeline.panel_scorer import PanelScorer  # noqa: PLC0415

    panel_cfg = (config or {}).get("ranking", {}).get("panel_scoring", {})
    comps_cfg = panel_cfg.get("components")
    if not isinstance(comps_cfg, list) or len(comps_cfg) < MIN_COMPONENTS:
        raise ValueError(
            "blend config requires ranking.panel_scoring.components as a "
            f"list of at least {MIN_COMPONENTS} entries, got: {comps_cfg!r}")
    strategy_dir = (config or {}).get("_strategy_dir")
    loaded: list[BlendComponent] = []
    for i, entry in enumerate(comps_cfg):
        if not isinstance(entry, dict):
            raise ValueError(f"blend component[{i}] must be a dict, got {entry!r}")
        comp_kind = entry.get("kind", PANEL_COMPONENT_KIND)
        if comp_kind == MOMENTUM_COMPONENT_KIND:
            loaded.append(_load_momentum_component(i, entry, strategy_dir))
            continue
        if comp_kind != PANEL_COMPONENT_KIND:
            # Inverted default (never enumerate-and-fall-through): an
            # unrecognized component kind is a refusal, not a classic leg.
            raise ValueError(
                f"blend component[{i}] declares unknown kind {comp_kind!r} — "
                f"supported: {PANEL_COMPONENT_KIND!r} (default, direct "
                f"artifact) or {MOMENTUM_COMPONENT_KIND!r} (ledger pointer); "
                "fail-closed")
        for key in REQUIRED_COMPONENT_KEYS:
            if not entry.get(key):
                raise ValueError(
                    f"blend component[{i}] missing required key {key!r} — "
                    "both identity pins are mandatory (fail-closed)")
        path = _resolve_component_path(str(entry["artifact_path"]), strategy_dir)
        full_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if not content_pin_matches(entry["expected_content_sha256"], full_sha):
            raise ValueError(
                f"blend component[{i}] content_sha256 MISMATCH for {path}: "
                f"pinned={entry['expected_content_sha256']!r} "
                f"observed=sha256:{full_sha}")
        scorer = PanelScorer.load(path)  # load errors propagate = fail closed
        if getattr(scorer, "requires_history", False):
            raise ValueError(
                f"blend component[{i}] ({path.name}) is a history-requiring "
                "scorer — blend components must be snapshot panel scorers")
        meta = getattr(scorer, "metadata", {}) or {}
        observed_fp = meta.get("config_fingerprint")
        if not config_fp_pin_matches(
                entry["expected_config_fingerprint"], observed_fp):
            raise ValueError(
                f"blend component[{i}] config_fingerprint MISMATCH for "
                f"{path}: pinned={entry['expected_config_fingerprint']!r} "
                f"observed={observed_fp!r}")
        loaded.append(BlendComponent(
            scorer=scorer,
            artifact_path=str(path),
            content_sha256="sha256:" + full_sha,
            config_fingerprint=str(observed_fp),
            trained_date=meta.get("trained_date"),
            effective_train_cutoff_date=(
                meta.get("effective_train_cutoff_date")
                or meta.get("trained_date")
            ),
        ))
        log.info(
            "load_blend_scorer: component[%d] %s verified "
            "(content=sha256:%s… fp=%s trained=%s)",
            i, path.name, full_sha[:16], observed_fp, meta.get("trained_date"))
    return BlendPanelScorer(loaded)


__all__ = [
    "BLEND_KIND",
    "MOMENTUM_COMPONENT_KIND",
    "PANEL_COMPONENT_KIND",
    "BlendComponent",
    "BlendPanelScorer",
    "composite_config_fingerprint",
    "config_fp_pin_matches",
    "content_pin_matches",
    "load_blend_scorer",
]
