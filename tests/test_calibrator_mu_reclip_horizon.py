"""R2 audit (latent): calibrated μ must be re-clipped after horizon up-scaling.

er_y is clipped to ±0.20 at load, but scaling μ from its native horizon to a
longer horizon multiplies it past that bound with no re-clip — breaking the
'Kelly numerator is bounded' invariant. No-op in prod (horizon == native = 60);
bites only when a horizon config is raised above native.
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _calibrator_expected_return_at_horizon,
)


class _CalWithHorizonKwarg:
    """Modern calibrator: scales internally via the horizon_days kwarg."""
    metadata = {"er_clip_bound": 0.20}

    def expected_return(self, raw, horizon_days=None):
        base = 0.18  # within bound at native (60d)
        return base if horizon_days is None else base * (horizon_days / 60.0)


class _OldCalNoKwarg:
    """Legacy calibrator: no horizon_days kwarg → the scaling fallback path."""
    metadata = {}

    def expected_return(self, raw):
        return 0.18


def test_no_scale_at_native_horizon() -> None:
    cal = _CalWithHorizonKwarg()
    assert _calibrator_expected_return_at_horizon(cal, 0.1, 60, 60) == pytest.approx(0.18)


def test_reclip_on_horizon_kwarg_path() -> None:
    cal = _CalWithHorizonKwarg()
    # 0.18 * (360/60) = 1.08 → clamped to the 0.20 bound
    assert _calibrator_expected_return_at_horizon(cal, 0.1, 360, 60) == pytest.approx(0.20)


def test_reclip_on_legacy_scaling_fallback() -> None:
    cal = _OldCalNoKwarg()
    # TypeError on kwarg → base 0.18 scaled ×6 → 1.08 → clamped to 0.20
    assert _calibrator_expected_return_at_horizon(cal, 0.1, 360, 60) == pytest.approx(0.20)
    # native horizon → no scale, returns base
    assert _calibrator_expected_return_at_horizon(cal, 0.1, 60, 60) == pytest.approx(0.18)


def test_reclip_respects_custom_bound() -> None:
    class _C(_CalWithHorizonKwarg):
        metadata = {"er_clip_bound": 0.10}
    assert _calibrator_expected_return_at_horizon(_C(), 0.1, 360, 60) == pytest.approx(0.10)
