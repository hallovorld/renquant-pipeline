"""P-SIZING-GATE-KEYS — campaign A3 (2026-07-03 design-compliance audit §5).

The divergent-defaults cluster: pipeline runtime defaults that CONTRADICT the
strategy-104 value for the same key. Losing a key silently flips live
semantics with green checks. This suite pins:

  * a prod-shaped config (every key present) passes,
  * each armed-and-missing key hard-fails, naming the key,
  * disarmed-and-missing keys pass with the exemption documented,
  * the documented fallback/alias keys satisfy the gate,
  * the check is PRESENCE-only — present-key values are never judged
    (present-key behavior byte-identical, protection contract),
  * the runtime `_kelly_sigma_horizon_days` present-key path is
    byte-identical to the pre-A3 implementation, and the missing-key path
    raises (defense in depth behind preflight).
"""
from __future__ import annotations

import copy
import math

import pytest

from renquant_pipeline.kernel.preflight import (
    _check_sizing_gate_keys,
    run_preflight,
)


def _prod_shaped_config() -> dict:
    """Mirror of the strategy-104 keys this gate covers (values as of main)."""
    return {
        "model_staleness_days": 60,
        "ranking": {
            "kelly_sizing": {
                "enabled": True,
                "fractional": 0.3,
                "max_concentration": 0.12,
                "topup_conviction_floor": 0.55,
                "sigma_horizon_days": 60,
            },
        },
        "rotation": {
            "enabled": True,
            "min_expected_advantage_pct": 0.06,
            "joint_actions": {
                "enabled": True,
                "qp_sigma_horizon_mode": "match_mu",
                "qp_sigma_unit": "annualized",
                "qp_horizon_contract": "strict",
                "qp_tax_lot_method": "hifo",
            },
        },
        "tax": {},
    }


def _drop(cfg: dict, dotted: str) -> dict:
    out = copy.deepcopy(cfg)
    node = out
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node[p]
    del node[parts[-1]]
    return out


def test_prod_shaped_config_passes() -> None:
    result = _check_sizing_gate_keys(_prod_shaped_config())

    assert result.name == "P-SIZING-GATE-KEYS"
    assert result.severity == "hard"
    assert result.ok is True
    assert result.details["missing_armed_keys"] == []


@pytest.mark.parametrize("dotted", [
    "ranking.kelly_sizing.fractional",
    "ranking.kelly_sizing.max_concentration",
    "ranking.kelly_sizing.topup_conviction_floor",
    "model_staleness_days",
    "rotation.min_expected_advantage_pct",
    "rotation.joint_actions.qp_sigma_horizon_mode",
    "rotation.joint_actions.qp_sigma_unit",
    "rotation.joint_actions.qp_horizon_contract",
    "rotation.joint_actions.qp_tax_lot_method",
])
def test_each_armed_missing_key_fails_closed_naming_the_key(dotted) -> None:
    result = _check_sizing_gate_keys(_drop(_prod_shaped_config(), dotted))

    assert result.ok is False
    assert result.severity == "hard"
    assert dotted in result.message
    assert dotted in result.details["missing_armed_keys"]


def test_multiple_missing_keys_all_reported() -> None:
    cfg = _drop(_prod_shaped_config(), "model_staleness_days")
    cfg = _drop(cfg, "ranking.kelly_sizing.max_concentration")

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is False
    assert set(result.details["missing_armed_keys"]) == {
        "model_staleness_days",
        "ranking.kelly_sizing.max_concentration",
    }


# ── disarmed consumers: absent keys are legitimate (documented pass) ────────

def test_kelly_keys_exempt_when_kelly_disabled() -> None:
    cfg = _prod_shaped_config()
    cfg["ranking"]["kelly_sizing"] = {"enabled": False}

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True
    assert "ranking.kelly_sizing.max_concentration" in (
        result.details["absent_but_disarmed_keys"])


def test_rotation_bar_exempt_when_rotation_disabled() -> None:
    cfg = _prod_shaped_config()
    cfg["rotation"]["enabled"] = False
    del cfg["rotation"]["min_expected_advantage_pct"]

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True
    assert "rotation.min_expected_advantage_pct" in (
        result.details["absent_but_disarmed_keys"])


def test_qp_keys_exempt_when_joint_actions_disabled() -> None:
    cfg = _prod_shaped_config()
    cfg["rotation"]["joint_actions"] = {
        "enabled": False,
        # qp_tax_lot_method stays REQUIRED even with joint_actions off
        # (trade_events builds sell events on every path) — keep it here.
        "qp_tax_lot_method": "hifo",
    }

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True
    assert "rotation.joint_actions.qp_sigma_horizon_mode" in (
        result.details["absent_but_disarmed_keys"])


def test_model_staleness_days_required_even_with_everything_disabled() -> None:
    """The staleness admission gate has no enable flag — absent ⇒ gate OFF.
    Audit 5.7: absent must fail closed regardless of other subsystems."""
    result = _check_sizing_gate_keys({})

    assert result.ok is False
    assert "model_staleness_days" in result.details["missing_armed_keys"]


def test_tax_lot_method_required_even_with_joint_actions_disabled() -> None:
    """trade_events._tax_lot_method reads the key on every sell path,
    independent of joint_actions.enabled — absence of BOTH the QP key and
    the tax.lot_method fallback must fail."""
    cfg = _prod_shaped_config()
    cfg["rotation"]["joint_actions"]["enabled"] = False
    del cfg["rotation"]["joint_actions"]["qp_tax_lot_method"]

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is False
    assert "rotation.joint_actions.qp_tax_lot_method" in (
        result.details["missing_armed_keys"])


# ── documented fallbacks / aliases satisfy the gate ─────────────────────────

def test_qp_mu_contract_legacy_alias_satisfies_horizon_contract() -> None:
    cfg = _drop(_prod_shaped_config(),
                "rotation.joint_actions.qp_horizon_contract")
    cfg["rotation"]["joint_actions"]["qp_mu_contract"] = "strict"

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True


def test_tax_lot_method_fallback_satisfies_qp_tax_lot_method() -> None:
    cfg = _drop(_prod_shaped_config(),
                "rotation.joint_actions.qp_tax_lot_method")
    cfg["tax"]["lot_method"] = "hifo"

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True


# ── protection contract: presence-only, values never judged ─────────────────

def test_present_key_values_are_never_judged() -> None:
    """The gate is a presence check ONLY — even divergent/odd present values
    pass it (present-key behavior stays byte-identical; value validation
    belongs to the consumers / other checks)."""
    cfg = _prod_shaped_config()
    cfg["model_staleness_days"] = 0            # explicit operator choice
    cfg["ranking"]["kelly_sizing"]["max_concentration"] = 0.35
    cfg["rotation"]["joint_actions"]["qp_horizon_contract"] = "warn"

    result = _check_sizing_gate_keys(cfg)

    assert result.ok is True


# ── wired into the preflight battery ────────────────────────────────────────

def test_run_preflight_includes_sizing_gate_keys(tmp_path) -> None:
    results = run_preflight(
        config=_drop(_prod_shaped_config(), "model_staleness_days"),
        broker=None,
        strategy_dir=tmp_path,
        strict=False,
    )

    by_name = {r.name: r for r in results}
    gate = by_name["P-SIZING-GATE-KEYS"]
    assert gate.ok is False and gate.severity == "hard"
    assert "model_staleness_days" in gate.message


def test_run_preflight_sizing_gate_passes_on_full_config(tmp_path) -> None:
    results = run_preflight(
        config=_prod_shaped_config(),
        broker=None,
        strategy_dir=tmp_path,
        strict=False,
    )

    by_name = {r.name: r for r in results}
    assert by_name["P-SIZING-GATE-KEYS"].ok is True


# ── runtime defense in depth: _kelly_sigma_horizon_days ─────────────────────

def _legacy_kelly_sigma_horizon_days(kelly_cfg: dict) -> float:
    """Verbatim pre-A3 implementation (minus the 252.0 absent default) —
    the reference for the present-key byte-identical regression."""
    raw = kelly_cfg.get("sigma_horizon_days", 252.0)
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(days) or days <= 0:
        return float("nan")
    return days


@pytest.mark.parametrize("raw", [
    60, 60.0, "60", 252, 252.0, 1, 0.5,          # valid values
    0, -1, float("nan"), float("inf"),           # invalid → nan (fail-closed)
    "abc", None, [], {},                          # unparseable → nan
    True, False,                                  # bools (float()-coerced)
])
def test_present_key_runtime_horizon_byte_identical(raw) -> None:
    from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
        _kelly_sigma_horizon_days,
    )

    new = _kelly_sigma_horizon_days({"sigma_horizon_days": raw})
    old = _legacy_kelly_sigma_horizon_days({"sigma_horizon_days": raw})

    assert (new == old) or (math.isnan(new) and math.isnan(old))


def test_missing_key_runtime_horizon_raises_pointing_at_preflight() -> None:
    from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
        _kelly_sigma_horizon_days,
    )

    with pytest.raises(RuntimeError) as exc:
        _kelly_sigma_horizon_days({})
    msg = str(exc.value)
    assert "ranking.kelly_sizing.sigma_horizon_days" in msg
    assert "P-KELLY-SIGMA-HORIZON" in msg
    assert "2026-06-11" in msg
    assert "252" in msg
