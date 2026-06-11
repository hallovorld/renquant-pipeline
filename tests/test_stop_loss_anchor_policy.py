"""Regression tests for BL-3: the stop-loss anchor policy must fail SAFE.

BL-3 (decision-tree deep audit, 2026-06-10): ``apply_stop_loss_anchor_policy``
used to ``raise ValueError`` when a holding's current- or entry-regime config
lacked a positive ``stop_loss_pct``. That raise runs inside the un-guarded list
comprehension ``[_make_sell_tctx(ctx, t) for t in _sell_universe(ctx)]`` in
pp_inference's sell passes, so ONE holding with a missing config field aborted
sell evaluation for the WHOLE book — every risk stop went dark for that bar.

These tests pin the fix: the policy degrades gracefully (never raises for
missing/zero stops) and the per-holding sell-tctx builder is wrapped so a
policy exception cannot take the whole-book sell pass down.
"""
from __future__ import annotations

from renquant_pipeline.kernel.pipeline.exit_params import (
    apply_stop_loss_anchor_policy,
)

_ANCHOR_CFG = {"risk": {"stop_loss_anchor_policy": {"mode": "max_entry_current"}}}


def test_anchor_widens_to_entry_stop_when_both_present() -> None:
    """Happy path: with both stops present, anchor keeps the wider (entry) stop."""
    exit_params = {"stop_loss_pct": 0.06}
    out = apply_stop_loss_anchor_policy(
        exit_params,
        config=_ANCHOR_CFG,
        current_regime="BULL_VOLATILE",
        entry_regime="BULL_CALM",
        entry_regime_params={"stop_loss_pct": 0.10},
    )
    assert out["stop_loss_pct"] == 0.10
    assert out["stop_loss_anchor_regime"] == "BULL_CALM"
    assert out["stop_loss_current_pct"] == 0.06
    assert out["stop_loss_entry_pct"] == 0.10


def test_missing_current_stop_does_not_raise() -> None:
    """BL-3: a holding with no current stop must NOT raise — degrade, preserve base."""
    exit_params: dict = {}  # no stop_loss_pct
    out = apply_stop_loss_anchor_policy(
        exit_params,
        config=_ANCHOR_CFG,
        current_regime="BULL_VOLATILE",
        entry_regime="BULL_CALM",
        entry_regime_params={"stop_loss_pct": 0.10},
    )
    # Returned unchanged: no anchor keys injected, no stop fabricated.
    assert out is exit_params
    assert "stop_loss_anchor_policy" not in out


def test_missing_entry_stop_keeps_current_stop() -> None:
    """BL-3: entry-regime config without a stop keeps the current stop, no raise."""
    exit_params = {"stop_loss_pct": 0.06}
    out = apply_stop_loss_anchor_policy(
        exit_params,
        config=_ANCHOR_CFG,
        current_regime="BULL_VOLATILE",
        entry_regime="BULL_CALM",
        entry_regime_params={},  # entry regime lacks stop_loss_pct
    )
    assert out["stop_loss_pct"] == 0.06  # current stop preserved, never crashed
    assert "stop_loss_anchor_policy" not in out


def test_non_positive_stops_degrade_not_crash() -> None:
    """Zero/negative/NaN stops are treated as missing — degrade, never raise."""
    for bad in (0.0, -0.05, float("nan"), float("inf"), None, "x"):
        out = apply_stop_loss_anchor_policy(
            {"stop_loss_pct": bad},
            config=_ANCHOR_CFG,
            current_regime="BULL_VOLATILE",
            entry_regime="BULL_CALM",
            entry_regime_params={"stop_loss_pct": 0.10},
        )
        # current stop unusable -> returns unchanged, no anchor applied
        assert "stop_loss_anchor_policy" not in out


def test_default_mode_is_passthrough() -> None:
    """Default (current_regime) mode never touches exit_params."""
    exit_params = {"stop_loss_pct": 0.06}
    out = apply_stop_loss_anchor_policy(
        exit_params,
        config={},  # no risk.stop_loss_anchor_policy -> default mode
        current_regime="BULL_VOLATILE",
        entry_regime="BULL_CALM",
        entry_regime_params={"stop_loss_pct": 0.10},
    )
    assert out is exit_params
    assert out == {"stop_loss_pct": 0.06}


def test_unknown_mode_still_raises_config_error() -> None:
    """A misspelled mode is a global config typo and SHOULD fail loudly.

    This is distinct from BL-3: an unknown mode is operator misconfiguration
    (affects every holding identically), not a per-holding data gap, so fail
    fast rather than silently degrade.
    """
    import pytest

    with pytest.raises(ValueError, match="unknown risk.stop_loss_anchor_policy.mode"):
        apply_stop_loss_anchor_policy(
            {"stop_loss_pct": 0.06},
            config={"risk": {"stop_loss_anchor_policy": {"mode": "typo_mode"}}},
            current_regime="BULL_VOLATILE",
            entry_regime="BULL_CALM",
            entry_regime_params={"stop_loss_pct": 0.10},
        )
