"""R2 audit (BL-4 class, sell-side): soft-exit thesis-age floor must not
silently fall through to 0 for regimes absent from min_holding_days_by_regime.

Prod sets {BULL_CALM: 60} with no flat min_holding_days, so before the fix the
guard returned 0 in BULL_VOLATILE/CHOPPY/BEAR — model-driven soft exits fired
with no minimum-hold protection (over-eager selling), especially for
broker-seeded holdings whose entry_regime is None.
"""
from __future__ import annotations

import logging

from renquant_pipeline.kernel.pipeline import soft_exit_guards as S


def _reset() -> None:
    S._MIN_DAYS_FALLTHROUGH_WARNED.clear()


def test_exact_regime_entry_wins() -> None:
    cfg = {"min_holding_days_by_regime": {"BULL_CALM": 60}}
    assert S._configured_min_days(cfg, "BULL_CALM") == 60


def test_default_key_covers_unlisted_regimes() -> None:
    cfg = {"min_holding_days_by_regime": {"BULL_CALM": 60, "default": 60}}
    assert S._configured_min_days(cfg, "BULL_VOLATILE") == 60
    assert S._configured_min_days(cfg, "BEAR") == 60
    assert S._configured_min_days(cfg, None) == 60
    assert S._configured_min_days(cfg, "BULL_CALM") == 60  # exact still wins


def test_missing_regime_no_default_warns(caplog) -> None:
    """The prod shape pre-config-fix: returns 0 (behavior preserved) BUT warns."""
    _reset()
    cfg = {"min_holding_days_by_regime": {"BULL_CALM": 60}}
    with caplog.at_level(logging.WARNING):
        val = S._configured_min_days(cfg, "BULL_VOLATILE")
    assert val == 0
    assert any("thesis-age soft-exit guard is OFF" in r.message
               for r in caplog.records)


def test_flat_global_used_when_set() -> None:
    cfg = {"min_holding_days_by_regime": {"BULL_CALM": 60}, "min_holding_days": 20}
    assert S._configured_min_days(cfg, "CHOPPY") == 20  # flat global, no warn


def test_no_map_uses_flat_global() -> None:
    assert S._configured_min_days({"min_holding_days": 10}, "BEAR") == 10
    assert S._configured_min_days({}, "BEAR") == 0


def test_horizon_suppression_holds_in_unlisted_regime_with_default() -> None:
    """End-to-end: with a default, a fresh holding in BEAR is still protected."""
    import datetime

    class _H:
        entry_date = datetime.date(2026, 6, 9)  # 1 trading day before today

    cfg = {"min_holding_days_by_regime": {"BULL_CALM": 60, "default": 60}}
    suppressed, reason = S.soft_exit_horizon_suppression(
        panel_cfg=cfg, regime="BEAR", today=datetime.date(2026, 6, 10),
        holding=_H())
    assert suppressed and "horizon_min_days" in reason
