"""ModelProtectionExitTask — thesis-aware N-of-N debounce in the sell chain.

Default OFF; emits a model_protection exit only after N consecutive μ breaches;
a recovering reading resets; never overrides a higher-priority exit; missing μ
never exits.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from renquant_pipeline.kernel.exits import ExitSignal, HoldingState
from renquant_pipeline.kernel.pipeline.task_sell import ModelProtectionExitTask


def _hs(expected_return, breaches=0):
    return HoldingState(
        entry_price=100.0, entry_date=dt.date.today(), high_watermark=110.0,
        prev_close=100.0, shares=10.0,
        expected_return=expected_return, protection_breaches=breaches,
    )


def _tc(hs, *, enabled=True, n_strikes=3, exit_signal=None):
    cfg = {"risk": {"model_protection": {
        "enabled": enabled, "exit_mu_threshold": 0.0, "n_strikes": n_strikes}}}
    return SimpleNamespace(
        config=cfg, holding=hs, ticker="AAA", today=dt.date.today(),
        regime="BULL_CALM", exit_params={}, exit_signal=exit_signal,
        prices={"AAA": 100.0})


def test_disabled_is_noop() -> None:
    hs = _hs(-0.5, breaches=2)
    tc = _tc(hs, enabled=False)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is None
    assert hs.protection_breaches == 2  # untouched


def test_positive_mu_holds_and_resets() -> None:
    hs = _hs(+0.03, breaches=2)
    tc = _tc(hs)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is None
    assert hs.protection_breaches == 0


def test_exits_only_on_third_consecutive_breach() -> None:
    hs = _hs(-0.01, breaches=0)
    for expected_breaches in (1, 2):
        tc = _tc(hs)
        ModelProtectionExitTask().run(tc)
        assert tc.exit_signal is None
        assert hs.protection_breaches == expected_breaches
    # third consecutive breach → exit
    tc = _tc(hs)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is not None
    assert tc.exit_signal.should_exit
    assert tc.exit_signal.exit_type == "model_protection"
    assert hs.protection_breaches == 0  # reset on exit


def test_recovery_resets_then_no_premature_exit() -> None:
    hs = _hs(-0.01, breaches=2)
    # a recovering reading wipes the streak
    hs.expected_return = +0.001
    ModelProtectionExitTask().run(_tc(hs))
    assert hs.protection_breaches == 0
    # next breach is strike 1, not an exit
    hs.expected_return = -0.01
    tc = _tc(hs)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is None and hs.protection_breaches == 1


def test_does_not_override_higher_priority_exit() -> None:
    hs = _hs(-0.5, breaches=2)
    prior = ExitSignal(should_exit=True, reason="stop_loss", exit_type="stop_loss")
    tc = _tc(hs, exit_signal=prior)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is prior  # unchanged
    assert hs.protection_breaches == 2  # not advanced


def test_missing_mu_never_exits() -> None:
    hs = _hs(None, breaches=2)
    tc = _tc(hs)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is None
    assert hs.protection_breaches == 2  # not advanced


def test_n_strikes_one_exits_immediately() -> None:
    hs = _hs(-0.01, breaches=0)
    tc = _tc(hs, n_strikes=1)
    ModelProtectionExitTask().run(tc)
    assert tc.exit_signal is not None
    assert tc.exit_signal.exit_type == "model_protection"
