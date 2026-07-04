"""Kelly sigma-horizon rescale regression tests."""
from __future__ import annotations

import math
import types
from dataclasses import dataclass

import pytest


@dataclass
class _Cand:
    ticker: str
    mu: float = 0.005
    sigma: float = 0.35
    kelly_target_pct: float | None = None


@dataclass
class _Hold:
    ticker: str
    mu: float = 0.005
    sigma: float = 0.35
    kelly_target_pct: float | None = None


def _ctx(*, sigma_horizon_days=None):
    kelly_cfg = {
        "enabled": True,
        "fractional": 0.5,
        "max_concentration": 1.0,
        "min_edge": 0.0,
    }
    if sigma_horizon_days is not None:
        kelly_cfg["sigma_horizon_days"] = sigma_horizon_days
    return types.SimpleNamespace(
        candidates=[_Cand("AAPL")],
        holdings={"MSFT": _Hold("MSFT")},
        config={
            "ranking": {"kelly_sizing": kelly_cfg},
            "regime_params": {"BULL_CALM": {"max_position_pct": 1.0}},
        },
        regime="BULL_CALM",
        confidence=1.0,
        counters={},
    )


def _run(ctx):
    from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
        ApplyKellySizingTask,
    )

    ApplyKellySizingTask().run(ctx)
    return ctx.candidates[0].kelly_target_pct, ctx.holdings["MSFT"].kelly_target_pct


def test_explicit_252_preserves_annualized_sigma_behavior() -> None:
    """Present-key regression pin: sigma_horizon_days=252 is byte-identical
    to the pre-A3 legacy default path."""
    explicit_ctx = _ctx(sigma_horizon_days=252)

    explicit_cand, explicit_hold = _run(explicit_ctx)

    expected = 0.5 * (0.005 / (0.35**2))
    assert explicit_cand == pytest.approx(expected)
    assert explicit_hold == pytest.approx(expected)


def test_missing_sigma_horizon_key_fails_loud_when_kelly_enabled() -> None:
    """Campaign A3 (audit §5.1 P0): the silent 252 default is gone. A missing
    key with kelly enabled must RAISE at scoring time (defense in depth
    behind preflight P-KELLY-SIGMA-HORIZON), never quietly re-arm the
    2026-06-11 variance bug."""
    implicit_ctx = _ctx()  # kelly enabled, sigma_horizon_days ABSENT

    with pytest.raises(RuntimeError, match="sigma_horizon_days") as exc:
        _run(implicit_ctx)
    assert "P-KELLY-SIGMA-HORIZON" in str(exc.value)
    assert "2026-06-11" in str(exc.value)


def test_missing_sigma_horizon_key_inert_when_kelly_disabled() -> None:
    """Kelly disabled ⇒ the task no-ops before the horizon read; a missing
    key must NOT raise (absent-is-legitimate when the consumer is off)."""
    ctx = _ctx()
    ctx.config["ranking"]["kelly_sizing"]["enabled"] = False

    cand_target, hold_target = _run(ctx)

    assert cand_target is None
    assert hold_target is None


def test_sigma_horizon_60_matches_mu_period_before_kelly_formula() -> None:
    legacy_ctx = _ctx(sigma_horizon_days=252)
    matched_ctx = _ctx(sigma_horizon_days=60)

    legacy_target, _ = _run(legacy_ctx)
    matched_target, matched_hold_target = _run(matched_ctx)

    expected_sigma_60d = 0.35 * math.sqrt(60.0 / 252.0)
    expected = 0.5 * (0.005 / (expected_sigma_60d**2))
    assert matched_target == pytest.approx(expected)
    assert matched_hold_target == pytest.approx(expected)
    assert matched_target == pytest.approx(legacy_target * (252.0 / 60.0))


def test_invalid_sigma_horizon_fails_closed_with_skip_reason() -> None:
    ctx = _ctx(sigma_horizon_days=0)

    cand_target, hold_target = _run(ctx)

    assert cand_target == 0.0
    assert hold_target == 0.0
    assert ctx._blocked_by_ticker["AAPL"] == "kelly_zero:sigma_horizon_invalid"
