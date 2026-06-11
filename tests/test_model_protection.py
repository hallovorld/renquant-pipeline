"""Foundation tests for the thesis-aware meta-label protection core.

Pure state machine: μ above τ holds (and resets strikes); μ <= τ accumulates
breaches; N consecutive breaches → exit; a missing μ never exits; disabled is
a no-op.
"""
from __future__ import annotations

from renquant_pipeline.kernel.model_protection import (
    ACTION_BREACH,
    ACTION_EXIT,
    ACTION_HOLD,
    ProtectionConfig,
    ProtectionState,
    evaluate,
    protection_config_from,
)

_ON = ProtectionConfig(enabled=True, exit_mu_threshold=0.0, n_strikes=3)


def test_disabled_is_noop() -> None:
    cfg = ProtectionConfig(enabled=False)
    action, st, _ = evaluate(-0.5, cfg, ProtectionState(2))
    assert action == ACTION_HOLD
    assert st.consecutive_breaches == 2  # untouched


def test_positive_mu_holds_and_resets() -> None:
    action, st, _ = evaluate(+0.03, _ON, ProtectionState(2))
    assert action == ACTION_HOLD
    assert st.consecutive_breaches == 0


def test_breach_accumulates_then_exits_on_third() -> None:
    st = ProtectionState(0)
    a1, st, _ = evaluate(-0.01, _ON, st)
    assert a1 == ACTION_BREACH and st.consecutive_breaches == 1
    a2, st, _ = evaluate(-0.02, _ON, st)
    assert a2 == ACTION_BREACH and st.consecutive_breaches == 2
    a3, st, reason = evaluate(-0.01, _ON, st)
    assert a3 == ACTION_EXIT and "thesis_breached" in reason
    assert st.consecutive_breaches == 0  # reset on exit


def test_recovery_clears_strikes() -> None:
    st = ProtectionState(0)
    _, st, _ = evaluate(-0.01, _ON, st)
    _, st, _ = evaluate(-0.01, _ON, st)
    assert st.consecutive_breaches == 2
    # one recovering reading wipes the count (CUSUM reset)
    _, st, _ = evaluate(+0.001, _ON, st)
    assert st.consecutive_breaches == 0
    # so the next breach is strike 1, not 3
    a, st, _ = evaluate(-0.01, _ON, st)
    assert a == ACTION_BREACH and st.consecutive_breaches == 1


def test_missing_mu_never_exits() -> None:
    for bad in (None, float("nan"), "x"):
        action, st, reason = evaluate(bad, _ON, ProtectionState(2))
        assert action == ACTION_HOLD
        assert st.consecutive_breaches == 2  # not advanced
        assert reason == "mu_unavailable"


def test_threshold_at_exactly_tau_is_a_breach() -> None:
    # μ == τ counts as a breach (<=)
    a, st, _ = evaluate(0.0, _ON, ProtectionState(0))
    assert a == ACTION_BREACH and st.consecutive_breaches == 1


def test_n_strikes_one_exits_immediately() -> None:
    cfg = ProtectionConfig(enabled=True, exit_mu_threshold=0.0, n_strikes=1)
    a, _, _ = evaluate(-0.01, cfg, ProtectionState(0))
    assert a == ACTION_EXIT


def test_config_reader_defaults_off() -> None:
    assert protection_config_from(None).enabled is False
    assert protection_config_from({}).enabled is False
    cfg = protection_config_from(
        {"risk": {"model_protection": {"enabled": True,
                                       "exit_mu_threshold": -0.01,
                                       "n_strikes": 5}}})
    assert cfg.enabled and cfg.exit_mu_threshold == -0.01 and cfg.n_strikes == 5


def test_config_reader_clamps_bad_values() -> None:
    cfg = protection_config_from(
        {"risk": {"model_protection": {"enabled": True, "n_strikes": 0,
                                       "exit_mu_threshold": "bad"}}})
    assert cfg.n_strikes == 1 and cfg.exit_mu_threshold == 0.0
