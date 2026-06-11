"""BL-2 regression: calibrator neutral-raw anchor + sign-consistency gate.

BL-2 (decision-tree deep audit, 2026-06-10): the calibrator maps a bearish raw
score to a positive μ because its ER=0 crossing sits near raw≈−0.13, not 0, and
there was NO stored neutral-raw anchor. These tests pin:

  1. ``GlobalPanelCalibration.neutral_raw`` reports the ER=0 crossing (incl. a
     negative-neutral calibrator and the all-positive "no crossing" surface).
  2. ``load`` stamps the anchor into metadata and warns on a non-trivial offset.
  3. ``long_signal_ok`` admits a long iff raw>0 AND (when present) ER>0 —
     blocking BOTH laundering directions, the single source of truth all
     admission paths route through.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)
from renquant_pipeline.kernel.pipeline.signal_direction import (
    REASON_NEGATIVE_RAW,
    REASON_NONPOSITIVE_ER,
    long_signal_ok,
)


# ── neutral_raw anchor ────────────────────────────────────────────────────────

def test_neutral_raw_interpolates_zero_crossing() -> None:
    cal = GlobalPanelCalibration(
        prob_x=np.array([-1.0, 0.0, 1.0]),
        prob_y=np.array([0.2, 0.5, 0.8]),
        er_x=np.array([-1.0, 0.0, 1.0]),
        er_y=np.array([-0.05, 0.0, 0.05]),
    )
    assert cal.neutral_raw == pytest.approx(0.0)
    assert cal.prob_neutral_raw == pytest.approx(0.0)


def test_neutral_raw_reports_negative_neutral_like_live_patchtst() -> None:
    """The BL-2 case: ER=0 sits at a NEGATIVE raw, so raw∈(neutral,0) → ER>0."""
    cal = GlobalPanelCalibration(
        prob_x=np.array([-0.30, -0.13, 0.10]),
        prob_y=np.array([0.40, 0.50, 0.62]),
        er_x=np.array([-0.30, -0.13, 0.10]),
        er_y=np.array([-0.04, 0.00, 0.05]),
    )
    assert cal.neutral_raw == pytest.approx(-0.13)
    # A slightly-negative raw maps to a positive ER — the laundering BL-2 names.
    assert cal.expected_return(-0.08) > 0.0


def test_neutral_raw_none_when_er_never_crosses_zero() -> None:
    """All-positive ER surface (scoring finding F3) → no anchor."""
    cal = GlobalPanelCalibration(
        prob_x=np.array([0.0, 1.0]),
        prob_y=np.array([0.55, 0.70]),
        er_x=np.array([0.0, 1.0]),
        er_y=np.array([0.01, 0.06]),
    )
    assert cal.neutral_raw is None


def test_load_stamps_anchor_and_warns_on_offset(tmp_path, caplog) -> None:
    artifact = tmp_path / "cal.json"
    artifact.write_text(
        json.dumps({
            "version": 1,
            "kind": "global_panel_calibration",
            "probability": {"x": [-0.30, -0.13, 0.10], "y": [0.40, 0.50, 0.62]},
            "expected_return": {"x": [-0.30, -0.13, 0.10],
                                 "y": [-0.04, 0.0, 0.05]},
            "metadata": {},
        }),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        cal = GlobalPanelCalibration.load(artifact)
    assert cal.metadata["neutral_raw_cached"] == pytest.approx(-0.13)
    assert any("neutral sits at raw" in r.message for r in caplog.records)


# ── long_signal_ok predicate ──────────────────────────────────────────────────

_CFG_ON = {"ranking": {"panel_scoring": {"enabled": True}}}


def test_block_negative_raw_even_with_positive_er() -> None:
    """The operator's case: never long a bearish raw, whatever ER says."""
    ok, reason = long_signal_ok(-0.08, _CFG_ON, expected_return=+0.03)
    assert not ok and reason == REASON_NEGATIVE_RAW


def test_block_nonpositive_er_even_with_positive_raw() -> None:
    """Inverse laundering: raw>0 but the calibrator says ER≤0 → block."""
    ok, reason = long_signal_ok(0.05, _CFG_ON, expected_return=-0.01)
    assert not ok and reason == REASON_NONPOSITIVE_ER


def test_admit_when_both_positive() -> None:
    ok, reason = long_signal_ok(0.05, _CFG_ON, expected_return=+0.02)
    assert ok and reason == ""


def test_missing_er_does_not_block_on_er_conjunct() -> None:
    """Calibrator off / μ unavailable → only the raw gate applies."""
    ok, reason = long_signal_ok(0.05, _CFG_ON, expected_return=None)
    assert ok and reason == ""


def test_gate_inert_when_panel_scoring_disabled() -> None:
    cfg = {"ranking": {"panel_scoring": {"enabled": False}}}
    ok, _ = long_signal_ok(-0.5, cfg, expected_return=-0.5)
    assert ok


def test_raw_gate_opt_out() -> None:
    cfg = {"ranking": {"panel_scoring": {
        "enabled": True, "require_positive_raw_signal_for_buy": False,
        "require_positive_expected_return_for_buy": False,
    }}}
    ok, _ = long_signal_ok(-0.5, cfg, expected_return=+0.01)
    assert ok
