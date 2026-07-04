from __future__ import annotations

from renquant_pipeline.kernel.preflight import (
    _check_kelly_sigma_horizon_config,
    run_preflight,
)


def _cfg(value=...):
    kelly = {"enabled": True}
    if value is not ...:
        kelly["sigma_horizon_days"] = value
    return {"ranking": {"kelly_sizing": kelly}}


def test_kelly_sigma_horizon_preflight_missing_key_fails_closed() -> None:
    """Campaign A3 (audit §5.1 P0): pre-fix this PASSED with "using default
    252" — losing the key silently re-armed the 2026-06-11 ~4.2x variance
    bug with green checks. Absent + kelly enabled must now hard-fail."""
    result = _check_kelly_sigma_horizon_config(_cfg())

    assert result.name == "P-KELLY-SIGMA-HORIZON"
    assert result.ok is False
    assert result.severity == "hard"
    assert "MISSING" in result.message
    assert "2026-06-11" in result.message
    assert result.details["removed_silent_default"] == 252.0


def test_kelly_sigma_horizon_preflight_missing_key_documented_when_disabled() -> None:
    """Absent-is-legitimate branch: kelly_sizing disabled ⇒ the value is
    never consumed, so absence passes — with the exemption documented."""
    cfg = _cfg()
    cfg["ranking"]["kelly_sizing"]["enabled"] = False

    result = _check_kelly_sigma_horizon_config(cfg)

    assert result.ok is True
    assert "unused" in result.message
    assert "REQUIRED" in result.message
    assert result.details["kelly_enabled"] is False


def test_kelly_sigma_horizon_preflight_accepts_positive_numeric() -> None:
    result = _check_kelly_sigma_horizon_config(_cfg("60"))

    assert result.ok is True
    assert result.details["sigma_horizon_days"] == 60.0


def test_kelly_sigma_horizon_preflight_rejects_non_positive() -> None:
    result = _check_kelly_sigma_horizon_config(_cfg(0))

    assert result.ok is False
    assert result.severity == "hard"
    assert "must be finite and > 0" in result.message


def test_kelly_sigma_horizon_preflight_rejects_bool() -> None:
    result = _check_kelly_sigma_horizon_config(_cfg(True))

    assert result.ok is False
    assert "got bool True" in result.message


def test_run_preflight_includes_sigma_horizon_gate(tmp_path) -> None:
    results = run_preflight(
        config=_cfg(float("nan")),
        broker=None,
        strategy_dir=tmp_path,
        strict=False,
    )

    by_name = {result.name: result for result in results}
    assert by_name["P-KELLY-SIGMA-HORIZON"].ok is False


# ── R2 audit: σ-horizon must match μ-horizon when Kelly uses calibrator μ ──

def _cfg_horizons(sigma_h, mu_h, *, use_cal_mu=True, via="qp"):
    kelly = {"enabled": True, "sigma_horizon_days": sigma_h,
             "use_calibrator_mu": use_cal_mu}
    cfg = {"ranking": {"kelly_sizing": kelly}}
    if mu_h is not None:
        if via == "qp":
            cfg["rotation"] = {"joint_actions": {"qp_mu_horizon_days": mu_h}}
        else:
            cfg["panel_ltr"] = {"lookahead_days": mu_h}
    return cfg


def test_sigma_mu_horizon_match_passes() -> None:
    r = _check_kelly_sigma_horizon_config(_cfg_horizons(60, 60))
    assert r.ok is True
    assert r.details["mu_horizon_days"] == 60


def test_sigma_mu_horizon_mismatch_fails_hard() -> None:
    """The exact original Kelly bug: σ=252 with a 60d μ."""
    r = _check_kelly_sigma_horizon_config(_cfg_horizons(252, 60))
    assert r.ok is False and r.severity == "hard"
    assert "μ horizon" in r.message
    assert r.details == {"sigma_horizon_days": 252.0, "mu_horizon_days": 60}


def test_mismatch_inert_without_calibrator_mu() -> None:
    """If Kelly does not consume calibrator μ, the σ/μ match is not enforced."""
    r = _check_kelly_sigma_horizon_config(
        _cfg_horizons(252, 60, use_cal_mu=False))
    assert r.ok is True


def test_mu_horizon_resolved_from_panel_ltr_lookahead() -> None:
    r = _check_kelly_sigma_horizon_config(_cfg_horizons(252, 60, via="panel_ltr"))
    assert r.ok is False and r.details["mu_horizon_days"] == 60
