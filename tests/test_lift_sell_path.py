"""Parity tests for the sell-path Tasks + Job (functional-lift).

Copy-and-rewrite. Lifts TickerSellJob + task_sell / task_panel_conviction_xs /
task_dd_flatten (task_benchmark_sleeve + soft_exit_guards landed with the
support layer). Pins import-cleanliness, the no-bare-kernel rewrite, and that
TickerSellJob wires a chain of real common.Task subclasses.
"""
from __future__ import annotations

import ast
import datetime as _dt
import importlib
from pathlib import Path
from types import SimpleNamespace as _NS

import pytest

import renquant_common
from renquant_pipeline.kernel.pipeline import soft_exit_guards as _S
from renquant_pipeline.kernel.pipeline.task_panel_conviction_xs import (
    CrossSectionalPanelExitTask,
)

PKG = "renquant_pipeline.kernel.pipeline."
KERNEL = Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"
MODULES = ["job_sell", "task_sell", "task_panel_conviction_xs", "task_dd_flatten"]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    assert importlib.import_module(PKG + module_name) is not None


def test_no_bare_kernel_import_survives() -> None:
    offenders: list[str] = []
    for m in MODULES:
        tree = ast.parse((KERNEL / "pipeline" / f"{m}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".", 1)[0] == "kernel":
                    offenders.append(f"{m}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(f"{m}: import {alias.name}")
    assert offenders == [], f"un-rewritten bare kernel imports: {offenders}"


def test_ticker_sell_job_wires_task_chain() -> None:
    job_sell = importlib.import_module(PKG + "job_sell")
    tasks = job_sell.TickerSellJob().tasks
    assert isinstance(tasks, list) and tasks
    for t in tasks:
        assert isinstance(t, renquant_common.Task)


# ─────────────────────────────────────────────────────────────────────────────
# CrossSectionalPanelExitTask per-regime trigger keys (orch#962 B1;
# BEAR-exit prereg 2026-08-08 §2).
#
# The two NEW keys — `xs_panel_percentile_floor_by_regime` and
# `mu_sell_ceiling_by_regime` — must mirror the EXISTING
# `min_holding_days_by_regime` resolution pattern
# (soft_exit_guards._configured_min_days): exact regime ->
# "default"/"_default" key -> flat scalar; non-dict/empty maps ignored.
# Each parity test below pins the EXISTING key's semantics first, then
# asserts the new keys match. A config without the new keys must behave
# byte-identically to the scalar-only read (behavior invariance).
# ─────────────────────────────────────────────────────────────────────────────

_TODAY = _dt.date(2026, 8, 10)


def _holding(panel_score, mu) -> _NS:
    # entry 5 calendar days ago: below the 30d LT tax gate window, and no
    # min_holding keys are set (horizon guard resolves to 0 → inactive).
    return _NS(
        panel_score=panel_score,
        mu=mu,
        entry_date=_TODAY - _dt.timedelta(days=5),
        entry_price=100.0,
        entry_regime=None,
        current_price=100.0,
    )


def _xs_ctx(panel_exit_cfg: dict, regime: str, holdings: dict, cand_scores: list):
    return _NS(
        config={"risk": {"panel_exit": panel_exit_cfg}},
        today=_TODAY,
        regime=regime,
        holdings=holdings,
        candidates=[
            _NS(ticker=f"C{i}", panel_score=s) for i, s in enumerate(cand_scores)
        ],
        exits=[],
        counters={},
        ohlcv={},
        earnings_calendar=None,
        prices={t: 100.0 for t in holdings},
    )


# Fixture A — regime-sensitivity. Cross-section (10 scores):
# [0.20, 0.35, 0.45, 0.55, 0.60, 0.65, 0.75, 0.85, 0.90, 0.95].
#   scalar/default floor 0.20 → idx=round(2.0)=2 → thr=0.45; LOWQ mu +0.005
#     fails mu_ceiling 0.0 → NO fire.
#   BEAR floor 0.35 → idx=round(3.5)=4 → thr=0.60; LOWQ 0.20 ≤ 0.60 AND
#     +0.005 ≤ +0.01 → fires.
_CAND_A = [0.35, 0.45, 0.55, 0.60, 0.65, 0.75, 0.85, 0.95]
_PREREG_MAPS = {
    "xs_panel_percentile_floor_by_regime": {"default": 0.20, "BEAR": 0.35},
    "mu_sell_ceiling_by_regime": {"default": 0.0, "BEAR": 0.01},
}


def _run_a(panel_exit_cfg: dict, regime: str):
    cfg = {"enabled": True, **panel_exit_cfg}
    ctx = _xs_ctx(
        cfg, regime,
        {"LOWQ": _holding(0.20, +0.005), "SAFE": _holding(0.90, +0.10)},
        _CAND_A,
    )
    CrossSectionalPanelExitTask().run(ctx)
    return ctx


# Fixture B — invariance. Cross-section (10 scores):
# [0.10, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 1.00].
# Scalar floor 0.20 → idx=2 → thr=0.50; DECAY (0.10, −0.02) fires the
# AND-rule; KEEP (0.85, +0.05) does not.
_CAND_B = [0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00]
_SCALAR_ONLY_B = {
    "enabled": True,
    "xs_panel_percentile_floor": 0.20,
    "mu_sell_ceiling": 0.0,
}
# Hand-derived from the pre-change scalar arithmetic (idx=round(10*0.20)=2,
# thr=sorted[2]=0.50) — the byte-identical expectation for any config that
# does not carry the new keys.
_EXPECTED_B_REASON = (
    "panel_conviction[xs] panel=+0.100 (thr=+0.500 of 10) mu=-0.0200"
)


def _run_b(panel_exit_cfg: dict, regime: str = "BEAR"):
    ctx = _xs_ctx(
        dict(panel_exit_cfg), regime,
        {"DECAY": _holding(0.10, -0.02), "KEEP": _holding(0.85, +0.05)},
        _CAND_B,
    )
    CrossSectionalPanelExitTask().run(ctx)
    return ctx


def _exit_shapes(ctx):
    return [
        (t, sig.exit_type, sig.should_exit, sig.reason) for t, sig in ctx.exits
    ]


def test_by_regime_exact_regime_overrides_fire_in_bear() -> None:
    # Existing-key pin: an exact regime entry wins over "default".
    assert _S._configured_min_days(
        {"min_holding_days_by_regime": {"default": 60, "BEAR": 10}}, "BEAR"
    ) == 10
    ctx = _run_a(_PREREG_MAPS, "BEAR")
    assert _exit_shapes(ctx) == [(
        "LOWQ", "panel_conviction", True,
        "panel_conviction[xs] panel=+0.200 (thr=+0.600 of 10) mu=+0.0050",
    )]
    assert ctx.counters.get("xs_panel_exit") == 1


def test_by_regime_absent_regime_resolves_to_default_key() -> None:
    # Existing-key pin: an unlisted regime resolves to the "default" entry.
    assert _S._configured_min_days(
        {"min_holding_days_by_regime": {"BULL_CALM": 60, "default": 60}}, "CHOPPY"
    ) == 60
    # New keys: CHOPPY resolves to default (0.20 / 0.0) → same no-fire
    # outcome as the scalar-only config.
    ctx_map = _run_a(_PREREG_MAPS, "CHOPPY")
    ctx_scalar = _run_a(
        {"xs_panel_percentile_floor": 0.20, "mu_sell_ceiling": 0.0}, "CHOPPY"
    )
    assert _exit_shapes(ctx_map) == _exit_shapes(ctx_scalar) == []
    assert ctx_map.counters == ctx_scalar.counters == {}


def test_by_regime_underscore_default_alias_matches_existing() -> None:
    # Existing-key pin: "_default" is honoured as the default alias.
    assert _S._configured_min_days(
        {"min_holding_days_by_regime": {"_default": 7}}, "CHOPPY"
    ) == 7
    ctx = _run_b({
        **_SCALAR_ONLY_B,
        "xs_panel_percentile_floor_by_regime": {"_default": 0.20},
        "mu_sell_ceiling_by_regime": {"_default": 0.0},
    })
    assert _exit_shapes(ctx) == [
        ("DECAY", "panel_conviction", True, _EXPECTED_B_REASON)
    ]


def test_by_regime_default_entries_equal_scalar_only() -> None:
    """Maps carrying only {"default": <scalar>} reproduce the scalar run."""
    ctx_scalar = _run_b(_SCALAR_ONLY_B)
    ctx_map = _run_b({
        **_SCALAR_ONLY_B,
        "xs_panel_percentile_floor_by_regime": {"default": 0.20},
        "mu_sell_ceiling_by_regime": {"default": 0.0},
    })
    assert _exit_shapes(ctx_scalar) == _exit_shapes(ctx_map) == [
        ("DECAY", "panel_conviction", True, _EXPECTED_B_REASON)
    ]
    assert ctx_scalar.counters == ctx_map.counters == {"xs_panel_exit": 1}


def test_by_regime_malformed_map_ignored_like_existing_key() -> None:
    # Existing-key pin FIRST: a non-dict map and an empty map are both
    # ignored — the flat scalar is used.
    assert _S._configured_min_days(
        {"min_holding_days_by_regime": "oops", "min_holding_days": 20}, "BEAR"
    ) == 20
    assert _S._configured_min_days(
        {"min_holding_days_by_regime": {}, "min_holding_days": 20}, "BEAR"
    ) == 20
    # New keys match: non-dict / empty maps fall back to the scalars.
    ctx = _run_b({
        **_SCALAR_ONLY_B,
        "xs_panel_percentile_floor_by_regime": "oops",
        "mu_sell_ceiling_by_regime": {},
    })
    assert _exit_shapes(ctx) == [
        ("DECAY", "panel_conviction", True, _EXPECTED_B_REASON)
    ]


def test_by_regime_malformed_value_skips_like_malformed_scalar() -> None:
    # Task's existing-behavior pin FIRST: a malformed SCALAR value makes the
    # task skip entirely (fail-safe: no false exit, no counters).
    ctx_scalar = _run_b({
        **_SCALAR_ONLY_B, "xs_panel_percentile_floor": "not-a-number",
    })
    assert ctx_scalar.exits == [] and ctx_scalar.counters == {}
    # New keys match: a malformed resolved per-regime value behaves the same.
    ctx_floor = _run_b({
        **_SCALAR_ONLY_B,
        "xs_panel_percentile_floor_by_regime": {"BEAR": "not-a-number"},
    })
    assert ctx_floor.exits == [] and ctx_floor.counters == {}
    ctx_mu = _run_b({
        **_SCALAR_ONLY_B,
        "mu_sell_ceiling_by_regime": {"BEAR": None},
    })
    assert ctx_mu.exits == [] and ctx_mu.counters == {}


def test_behavior_invariance_without_new_keys() -> None:
    """Fix-wave invariance: configs that do NOT carry the new keys keep
    today's behavior byte-identically — same exits, same reason strings,
    same counters — in every regime (the scalar read ignores regime)."""
    for regime in ("BULL_CALM", "BULL_VOLATILE", "CHOPPY", "BEAR"):
        ctx = _run_b(_SCALAR_ONLY_B, regime=regime)
        assert _exit_shapes(ctx) == [
            ("DECAY", "panel_conviction", True, _EXPECTED_B_REASON)
        ], regime
        assert ctx.counters == {"xs_panel_exit": 1}, regime
        # Fixture A's no-fire side under the same scalar-only read.
        ctx_a = _run_a(
            {"xs_panel_percentile_floor": 0.20, "mu_sell_ceiling": 0.0}, regime
        )
        assert ctx_a.exits == [] and ctx_a.counters == {}, regime
