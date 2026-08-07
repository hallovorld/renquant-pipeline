"""The replay's per-name cap must be the production cap, or say it is not.

Measured 2026-08-06 (#271): `_MAX_POSITION_PCT_BY_REGIME` in the replay loader
has never matched production. `BULL_CALM` sat at 0.15 against a prod 0.12, and
`BULL_VOLATILE` / `CHOPPY` / `BEAR` are absent from the dict entirely so all
three fall through to `_DEFAULT_MAX_POSITION_PCT = 0.20` — a coincidental match
for BULL_VOLATILE, wrong for CHOPPY (0.15), and badly wrong for BEAR, whose
config says hold nothing while replay sizes at 20%.

The separation is a legitimate design (replay defaults are not prod
constraints). The defect was that it was SILENT: a replay run used to justify a
sizing change measured a different book and reported nothing about it.

`BEAR` is the load-bearing case below — a cap of 0.0 is a real instruction, and
any `or`/truthiness handling in the resolution chain turns it back into 0.20.
"""
from __future__ import annotations

import logging

import pytest

from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
    _DEFAULT_MAX_POSITION_PCT,
    _build_snapshot,
    _max_position_pct_for_regime,
)

# The shape strategy_config actually has, per the live file.
PROD = {
    "position_sizing": {"max_position_pct": 0.15},
    "regime_params": {
        "BULL_CALM": {"max_position_pct": 0.12},
        "BULL_VOLATILE": {"max_position_pct": 0.20},
        "CHOPPY": {"max_position_pct": 0.15},
        "BEAR": {"max_position_pct": 0.0},
    },
}


# ── with config: the production number, every regime ───────────────────────

@pytest.mark.parametrize("regime,expected", [
    ("BULL_CALM", 0.12),
    ("BULL_VOLATILE", 0.20),
    ("CHOPPY", 0.15),
    ("BEAR", 0.0),
])
def test_resolves_the_production_cap(regime, expected):
    assert _max_position_pct_for_regime(regime, PROD) == expected


def test_bear_zero_is_honoured_not_treated_as_unset():
    """The whole point. 0.0 means hold nothing; the pre-fix path gave BEAR the
    0.20 fallback, i.e. replay took 20% positions in the regime the config
    forbids holding anything in."""
    assert _max_position_pct_for_regime("BEAR", PROD) == 0.0
    assert _max_position_pct_for_regime("BEAR", PROD) != _DEFAULT_MAX_POSITION_PCT


def test_bull_calm_no_longer_returns_the_stale_015():
    assert _max_position_pct_for_regime("BULL_CALM", PROD) == 0.12
    assert _max_position_pct_for_regime("BULL_CALM", PROD) != 0.15


def test_tracks_a_raised_cap_rather_than_a_pinned_constant():
    """strategy-104#94 raises BULL_CALM to 0.30. A replay that still sized at
    0.15 could not validate it — the reason #271 blocks that PR's evidence."""
    raised = {"regime_params": {"BULL_CALM": {"max_position_pct": 0.30}}}
    assert _max_position_pct_for_regime("BULL_CALM", raised) == 0.30


def test_position_sizing_section_is_dead_in_production_and_here():
    """Measured 2026-08-06: all six production read sites are a bare
    `regime_params.get("max_position_pct", 0.15)` — none consults
    `position_sizing.max_position_pct`. A first cut of this fix routed through
    `resolve_regime_knob` and honoured that section, which would have made
    replay diverge from production in a NEW way while claiming to fix the old
    one. This test pins the measured contract, not the plausible one."""
    cfg = {
        "position_sizing": {"max_position_pct": 0.11},
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.12}},
    }
    assert _max_position_pct_for_regime("BULL_CALM", cfg) == 0.12   # not 0.11


def test_regime_present_but_capless_takes_the_production_fallback():
    """Production's literal fallback is 0.15, repeated at six call sites."""
    cfg = {"position_sizing": {"max_position_pct": 0.11},
           "regime_params": {"BULL_CALM": {"stop_loss_pct": 0.15}}}
    assert _max_position_pct_for_regime("BULL_CALM", cfg) == 0.15   # not 0.11


# ── without config: unchanged behaviour, but audible ───────────────────────

def test_no_config_keeps_the_legacy_defaults():
    # Every existing caller passes nothing; none of them may change behaviour.
    assert _max_position_pct_for_regime("BULL_CALM") == 0.15
    assert _max_position_pct_for_regime("CHOPPY") == _DEFAULT_MAX_POSITION_PCT
    assert _max_position_pct_for_regime(None) == _DEFAULT_MAX_POSITION_PCT


def test_no_config_warns_that_the_cap_is_not_production(caplog):
    with caplog.at_level(logging.WARNING):
        _max_position_pct_for_regime("BULL_CALM")
    msg = caplog.text
    assert "REPLAY DEFAULT" in msg
    assert "not production" in msg


def test_config_path_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        _max_position_pct_for_regime("BULL_CALM", PROD)
    assert "REPLAY DEFAULT" not in caplog.text


def test_regime_missing_from_a_supplied_config_warns(caplog):
    with caplog.at_level(logging.WARNING):
        cap = _max_position_pct_for_regime("NEW_REGIME", PROD)
    assert cap == _DEFAULT_MAX_POSITION_PCT
    assert "NOT sized like production" in caplog.text


# ── malformed input ────────────────────────────────────────────────────────

def test_non_numeric_cap_falls_back_and_says_so(caplog):
    bad = {"regime_params": {"BULL_CALM": {"max_position_pct": "wide"}}}
    with caplog.at_level(logging.WARNING):
        cap = _max_position_pct_for_regime("BULL_CALM", bad)
    assert cap == 0.15                      # the replay default for BULL_CALM
    assert "non-numeric" in caplog.text


@pytest.mark.parametrize("cfg", [None, {}, {"regime_params": None}])
def test_empty_or_broken_config_does_not_raise(cfg):
    assert _max_position_pct_for_regime("BULL_CALM", cfg) == 0.15


# ── threaded through the snapshot ──────────────────────────────────────────

def test_snapshot_uses_the_configured_cap():
    snap = _build_snapshot(["AAPL", "MSFT"], "BULL_CALM", strategy_config=PROD)
    assert list(snap.w_upper) == [0.12, 0.12]
    assert list(snap.w_upper_hard) == [0.12, 0.12]


def test_snapshot_without_config_is_unchanged():
    snap = _build_snapshot(["AAPL", "MSFT"], "BULL_CALM")
    assert list(snap.w_upper) == [0.15, 0.15]


def test_sector_cap_vector_scales_off_the_configured_cap():
    # _build_sector_matrix multiplies per_name_cap by max_per_sector; a stale
    # per-name cap silently mis-scales the sector ceiling too.
    snap = _build_snapshot(
        ["AAPL", "MSFT"], "BULL_CALM",
        sector_map={"AAPL": "Tech", "MSFT": "Tech"}, max_per_sector=2,
        strategy_config=PROD,
    )
    assert snap.sector_cap_vec is not None
    assert list(snap.sector_cap_vec) == [pytest.approx(0.24)]   # 2 x 0.12


def test_loader_accepts_the_kwarg():
    import inspect

    from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
        load_replay_bars_from_sim_db,
    )
    sig = inspect.signature(load_replay_bars_from_sim_db)
    assert "strategy_config" in sig.parameters
    assert sig.parameters["strategy_config"].default is None


def test_the_docstring_no_longer_claims_it_mirrors_prod():
    """The module docstring asserted these defaults 'mirror the per-regime
    conviction cap range used by prod'. They never did — an asserted quantity
    that was never measured is how this survived."""
    from renquant_pipeline.kernel.portfolio_qp import wf_replay_loader

    doc = wf_replay_loader.__doc__ or ""
    assert "Mirrors\n  the per-regime conviction cap range used by prod" not in doc
    assert "never did" in doc
