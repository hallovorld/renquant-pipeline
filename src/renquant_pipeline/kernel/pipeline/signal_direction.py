"""Single source of truth for the long signal-direction gate.

BL-2 (decision-tree deep audit, 2026-06-10). The system must never open a
long on a name the model is bearish on, regardless of what the calibrator
extrapolates for μ / expected-return. Two failure directions exist:

  1. ``raw < 0`` but the calibrator launders it to ``μ > 0`` (the calibrator's
     ER=0 neutral sits near raw≈−0.13, so a slightly-negative raw maps to a
     positive expected return). The operator's case: "longing a negative-signal
     ticker is insane." Block on raw sign.
  2. ``raw > 0`` but the calibrator says ``expected_return ≤ 0`` (a calibrator
     whose neutral sits ABOVE 0). The inverse laundering. Block on ER sign.

The robust rule is the CONJUNCTION: admit a long iff the raw model signal is
bullish AND (when a calibrated expected return is available) it is positive.
This holds regardless of where the calibrator's neutral_raw anchor sits, so it
does not depend on trusting a single magic threshold.

This predicate is intentionally dependency-free so every admission path
(SizeAndEmit, QP buy-leg, rotation, top-up) can import and apply the SAME
rule — the audit's BL-4/B3/B4 single-path-coverage gap is closed by routing
all of them through here.
"""
from __future__ import annotations

import math
from typing import Any

# Block reasons (stable strings for telemetry / decision-trace).
REASON_NEGATIVE_RAW = "negative_raw_signal_no_long"
REASON_NONPOSITIVE_ER = "nonpositive_expected_return_no_long"


def require_positive_raw_signal_cfg(config: dict | None) -> bool:
    """Whether new longs require a positive raw panel_score.

    Default ON when panel scoring is enabled. Opt out with
    ``ranking.panel_scoring.require_positive_raw_signal_for_buy: false``. When
    the run uses a non-panel ranker there is no raw panel_score contract to
    enforce, so the gate is inert.
    """
    sel = ((config or {}).get("ranking", {}) or {}).get("panel_scoring", {}) or {}
    if not bool(sel.get("enabled", False)):
        return False
    v = sel.get("require_positive_raw_signal_for_buy")
    return True if v is None else bool(v)


def _require_positive_er_cfg(config: dict | None) -> bool:
    """Whether new longs also require a positive calibrated expected return.

    Default ON (alongside the raw gate) when panel scoring is enabled. The ER
    conjunct only bites when an expected return is actually present on the
    candidate; it is a no-op when the calibrator is off / μ unavailable. Opt
    out with ``ranking.panel_scoring.require_positive_expected_return_for_buy:
    false``.
    """
    sel = ((config or {}).get("ranking", {}) or {}).get("panel_scoring", {}) or {}
    if not bool(sel.get("enabled", False)):
        return False
    v = sel.get("require_positive_expected_return_for_buy")
    return True if v is None else bool(v)


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def long_signal_ok(
    panel_score: Any,
    config: dict | None,
    *,
    expected_return: Any = None,
) -> tuple[bool, str]:
    """Return ``(admitted, reason)`` for opening a NEW long.

    ``reason`` is ``""`` when admitted. Pass the calibrated ``expected_return``
    (or μ) when available so the ER-sign conjunct can apply; omit it (or pass
    ``None``) and only the raw-sign gate is enforced.
    """
    if require_positive_raw_signal_cfg(config):
        ps = _finite(panel_score)
        if ps is None or ps <= 0.0:
            return False, REASON_NEGATIVE_RAW
    if _require_positive_er_cfg(config):
        er = _finite(expected_return)
        # Only enforce when an expected return is actually present; a missing
        # ER (calibrator off) must not block on this conjunct.
        if er is not None and er <= 0.0:
            return False, REASON_NONPOSITIVE_ER
    return True, ""


def long_signal_ok_for_object(obj: Any, config: dict | None) -> tuple[bool, str]:
    """Apply :func:`long_signal_ok` to a candidate/holding-like object."""
    if obj is None:
        return long_signal_ok(None, config, expected_return=None)
    expected_return = getattr(obj, "expected_return", None)
    if expected_return is None:
        expected_return = getattr(obj, "mu", None)
    return long_signal_ok(
        getattr(obj, "panel_score", None),
        config,
        expected_return=expected_return,
    )


__all__ = [
    "REASON_NEGATIVE_RAW",
    "REASON_NONPOSITIVE_ER",
    "require_positive_raw_signal_cfg",
    "long_signal_ok",
    "long_signal_ok_for_object",
]
