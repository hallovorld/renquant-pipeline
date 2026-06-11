"""H-1 regression: single-day-loss gate anchors to the ENTRY regime (opt-in).

H-1 (decision-tree deep audit, 2026-06-10): SDL params are read from the
CURRENT regime, so a BULL_CALM thesis (σ-adaptive sdl_n_sigma=3, no absolute
cap) re-labeled BULL_VOLATILE inherits that regime's tight absolute 6%
single-day stop — a whipsaw on a position whose thesis never changed.

apply_single_day_loss_anchor_policy (opt-in mode=entry_regime) sources the SDL
config from the entry thesis so a relabel cannot retighten it.
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.pipeline.exit_params import (
    apply_single_day_loss_anchor_policy,
)

# The documented whipsaw: entered BULL_CALM, now re-labeled BULL_VOLATILE.
_BULL_CALM = {"max_single_day_loss_pct": 0, "sdl_n_sigma": 3}
_CURRENT_BULL_VOLATILE = {"max_single_day_loss_pct": 0.06, "sdl_n_sigma": 0}


def _anchor(exit_params, *, mode, current="BULL_VOLATILE", entry="BULL_CALM",
            entry_params=None):
    cfg = {"risk": {"sdl_anchor_policy": {"mode": mode}}}
    return apply_single_day_loss_anchor_policy(
        exit_params,
        config=cfg,
        current_regime=current,
        entry_regime=entry,
        entry_regime_params=entry_params if entry_params is not None else _BULL_CALM,
    )


def test_default_mode_is_passthrough() -> None:
    ep = dict(_CURRENT_BULL_VOLATILE)
    out = apply_single_day_loss_anchor_policy(
        ep, config={}, current_regime="BULL_VOLATILE",
        entry_regime="BULL_CALM", entry_regime_params=_BULL_CALM,
    )
    assert out["max_single_day_loss_pct"] == 0.06  # unchanged
    assert out["sdl_n_sigma"] == 0


def test_entry_regime_mode_restores_thesis_sdl() -> None:
    """The fix: the BULL_CALM thesis keeps its σ-adaptive SDL, not the tight
    BULL_VOLATILE absolute 6% it was relabeled into."""
    ep = dict(_CURRENT_BULL_VOLATILE)
    out = _anchor(ep, mode="entry_regime")
    assert out["max_single_day_loss_pct"] == 0   # absolute cap off (entry)
    assert out["sdl_n_sigma"] == 3               # σ-adaptive restored
    assert out["sdl_anchor_regime"] == "BULL_CALM"
    assert out["sdl_current_regime"] == "BULL_VOLATILE"


def test_no_entry_regime_is_noop() -> None:
    ep = dict(_CURRENT_BULL_VOLATILE)
    out = apply_single_day_loss_anchor_policy(
        ep, config={"risk": {"sdl_anchor_policy": {"mode": "entry_regime"}}},
        current_regime="BULL_VOLATILE", entry_regime=None,
        entry_regime_params=_BULL_CALM,
    )
    assert out["max_single_day_loss_pct"] == 0.06  # untouched


def test_absent_entry_keys_keep_current_value() -> None:
    """An entry regime that doesn't define an SDL key cannot invent/clear it —
    the current value is preserved (anchoring only loosens via real config)."""
    ep = {"max_single_day_loss_pct": 0.06, "sdl_n_sigma": 0}
    out = _anchor(ep, mode="entry_regime", entry_params={"sdl_n_sigma": 4})
    assert out["sdl_n_sigma"] == 4               # overridden from entry
    assert out["max_single_day_loss_pct"] == 0.06  # absent in entry → kept


def test_entry_regime_filter_scopes_the_policy() -> None:
    """entry_regimes allowlist: policy only fires for listed entry theses."""
    cfg = {"risk": {"sdl_anchor_policy": {
        "mode": "entry_regime", "entry_regimes": ["BULL_CALM"]}}}
    ep = dict(_CURRENT_BULL_VOLATILE)
    # entry regime CHOPPY is not in the allowlist → no anchoring
    out = apply_single_day_loss_anchor_policy(
        ep, config=cfg, current_regime="BULL_VOLATILE",
        entry_regime="CHOPPY", entry_regime_params={"sdl_n_sigma": 9},
    )
    assert out["sdl_n_sigma"] == 0  # unchanged (CHOPPY filtered out)


def test_unknown_mode_raises_config_error() -> None:
    with pytest.raises(ValueError, match="unknown risk.sdl_anchor_policy.mode"):
        _anchor(dict(_CURRENT_BULL_VOLATILE), mode="typo")
