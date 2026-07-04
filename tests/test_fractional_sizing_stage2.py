"""S-FRAC v2 STAGE 2 (2026-07-03): notional-exact fractional sizing under a
flag, superseding A-3's one-share round-up when enabled.

Design: renquant-orchestrator doc/design/2026-07-02-s-frac-fractional-v2.md
§6 stage 2 / §7.2 (supersession + fallback) / §7.4 (sizing-fidelity KPI).
Sizing core + fail-closed config reader salvaged from the preserved
pipeline#153 branch; the supersession seam against #156's one-share floor and
the ledger schema are new stage-2 work.

The canonical fixture is the 2026-07-02 forensics case: a $381 compounded
target on a $1,100 name (BLK-class). The three sizing arms (§7.5):

  A  whole-share        int(381/1100) == 0            → DROPPED (size_insufficient_cash)
  B  one_share_floor    rounds UP to 1 share = $1,100 → ≈190% overshoot of target
  C  fractional         floor6dp(381/1100) = 0.346363 → $380.9993 ≈ target (gap ≈ 0)

Invariant pinned throughout: fractional sizing changes HOW MUCH of an
admitted name is bought, never WHETHER — the admission/gate/veto/conviction
path is identical across every flag state (§1.3, §6 stage-2 kill condition).
"""
from __future__ import annotations

import copy
import datetime as dt

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask

# BLK-class fixture: PV $10k, ample cash, regime cap 15% ($1,500 ≥ 1 share of
# BLK so the A-3 floor is eligible), conviction compounding the target to
# 0.15 × 0.254 = 3.81% of PV = $381.00 < one $1,100 share.
PV = 10_000.0
CASH = 5_000.0
BLK_PRICE = 1_100.0
REGIME_CAP_PCT = 0.15
CONV_MIN_MULT = 0.254
TARGET = REGIME_CAP_PCT * CONV_MIN_MULT * PV          # == $381.00 (fp ≈)
BLK_FRACTIONAL_QTY = 0.346363                          # floor6dp(381/1100)


def _cand(ticker, panel_score=0.001, *, expected_return=0.04, mu=0.04, sigma=0.2):
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=expected_return,
        expected_return_horizon_days=60,
        panel_score=panel_score, mu=mu, mu_horizon_days=60, sigma=sigma,
    )


def _config(*, fractional=None, one_share_floor=None, conv_min_mult=CONV_MIN_MULT):
    """Config compounding max_pct = 0.15 × conv_min_mult (score below floor)."""
    cfg = {
        "regime_params": {"BULL_CALM": {
            "max_position_pct": REGIME_CAP_PCT,
            "cash_reserve_pct": 0.0,
            "max_concurrent_positions": 8,
        }},
        "ranking": {"panel_scoring": {
            "enabled": True,
            "sizing": {"enabled": True, "floor": 0.5, "ceiling": 1.0,
                       "min_mult": conv_min_mult},
            "sigma_sizing": {},
        }, "kelly_sizing": {"enabled": False}},
        "regime": {},
    }
    if one_share_floor is not None:
        cfg["sizing"] = {"one_share_floor_enabled": one_share_floor}
    if fractional is not None:
        cfg["execution"] = {"fractional_shares": fractional}
    return cfg


def _ctx(ranked, selected, config, *, cash=CASH, pv=PV, prices=None, **overrides):
    values = {
        "config": config, "today": dt.date(2026, 7, 2), "regime": "BULL_CALM",
        "confidence": 1.0, "bear_only": False, "portfolio_value": pv,
        "cash": cash,
        "prices": prices or {c.ticker: BLK_PRICE for c in ranked},
        "ranked": ranked, "models": {},
    }
    values.update(overrides)
    ctx = InferenceContext(**values)
    ctx._selected = selected  # noqa: SLF001
    return ctx


def _run(config, *, tickers=("BLK",), prices=None):
    ranked = [_cand(t) for t in tickers]
    ctx = _ctx(ranked, list(tickers), config, prices=prices)
    SizeAndEmitTask().run(ctx)
    return ctx


# ── The three-arm BLK fixture (§7.5 comparison arms, frozen) ─────────────────


def test_arm_a_whole_share_drops_blk():
    """Arm A (status quo): $381 target < 1 × $1,100 share ⇒ dropped."""
    ctx = _run(_config())
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"  # noqa: SLF001


def test_arm_b_one_share_floor_overshoots_190pct():
    """Arm B (A-3): rounds UP to exactly 1 share — a ≈190% overshoot of the
    $381 risk-budget target, now ledger-visible via the §7.4 fields."""
    ctx = _run(_config(one_share_floor=True))
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["shares"] == 1
    assert order["invest"] == pytest.approx(BLK_PRICE)
    assert order["size_floor_reason"] == "one_share_floor_round_up"
    # §7.4 KPI fields on the A-3 arm (stage-2 schema):
    assert order["sizing_mode"] == "one_share_floor"
    assert order["target_notional"] == pytest.approx(TARGET)
    assert order["realized_notional_planned"] == pytest.approx(BLK_PRICE)
    gap = abs(order["realized_notional_planned"] - order["target_notional"]) \
        / order["target_notional"]
    assert gap == pytest.approx(1.887, abs=0.01)     # ≈ +189% overshoot
    assert ctx.counters["one_share_floor_roundups"] == 1


def test_arm_c_fractional_realizes_target_exactly():
    """Arm C (stage 2): 0.346363 shares ≈ $381.00 — sizing_fidelity_gap ≈ 0
    (§7.4 stage-2 target: median gap ≤ 1%; this fixture achieves ~2e-6)."""
    ctx = _run(_config(fractional={"enabled": True}))
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["shares"] == BLK_FRACTIONAL_QTY
    assert order["invest"] == pytest.approx(381.0, abs=1e-3)
    assert order["sizing_mode"] == "fractional"
    assert order["target_notional"] == pytest.approx(TARGET)
    assert order["realized_notional_planned"] == pytest.approx(381.0, abs=1e-3)
    gap = abs(order["realized_notional_planned"] - order["target_notional"]) \
        / order["target_notional"]
    assert gap <= 0.01                                # §7.4 stage-2 AC
    # Never a round-up: realized ≤ target (6dp floor semantics).
    assert order["realized_notional_planned"] <= order["target_notional"] + 1e-9
    # No A-3 artifacts on the fractional path.
    assert "size_floor_reason" not in order
    assert "one_share_floor_roundups" not in ctx.counters
    assert "BLK" not in getattr(ctx, "_blocked_by_ticker", {})


# ── Flag-off byte-inertness (regression pin) ─────────────────────────────────


def _orders_snapshot(config, *, tickers=("OXY", "BLK"), prices=None):
    prices = prices or {"OXY": 48.0, "BLK": BLK_PRICE}
    ctx = _run(config, tickers=tickers, prices=prices)
    return copy.deepcopy(ctx.orders), dict(getattr(ctx, "_blocked_by_ticker", {}))


def test_flag_off_byte_inert():
    """Absent / false / fail-closed-malformed fractional config ⇒ order dicts
    and block reasons BYTE-IDENTICAL to a config with no execution block."""
    baseline_orders, baseline_blocked = _orders_snapshot(_config())
    for frac in (
        None,                                   # no execution block at all
        {"enabled": False},                     # explicit off
        {"enabled": "true"},                    # YAML string — fails CLOSED
        {"enabled": 1},                         # non-bool — fails CLOSED
        {},                                     # empty block
    ):
        orders, blocked = _orders_snapshot(_config(fractional=frac))
        assert orders == baseline_orders
        assert blocked == baseline_blocked
    # And the baseline itself gained no stage-2 fields.
    assert len(baseline_orders) == 1            # OXY whole-share order
    for key in ("sizing_mode", "target_notional", "realized_notional_planned"):
        assert key not in baseline_orders[0]
    assert baseline_blocked["BLK"] == "size_insufficient_cash"
    # Whole-share quantities stay ints when the flag is off.
    assert isinstance(baseline_orders[0]["shares"], int)


# ── Dust guard (§7.3 / open question §9.5 — $25 default, never a $0 admit) ───


def test_fractional_dust_skip_below_floor():
    """A sized fractional entry below max($1 broker floor, $25 anti-churn)
    is SKIPPED with the dedicated reason — never admitted as a ~$0 order."""
    # conv_min_mult 0.01 → target 0.15×0.01×10k = $15 < $25 ⇒ dust.
    cfg = _config(fractional={"enabled": True}, conv_min_mult=0.01)
    ctx = _run(cfg, tickers=("OXY",), prices={"OXY": 100.0})
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["OXY"] == "fractional_dust_skip"  # noqa: SLF001
    assert ctx.counters["selection_fractional_dust_skip"] == 1


def test_fractional_dust_floor_operator_override():
    """Lowering min_fractional_trade_notional to $10 admits the $15 entry."""
    cfg = _config(
        fractional={"enabled": True, "min_fractional_trade_notional": 10.0},
        conv_min_mult=0.01,
    )
    ctx = _run(cfg, tickers=("OXY",), prices={"OXY": 100.0})
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["shares"] == pytest.approx(0.15, abs=1e-6)
    assert order["invest"] == pytest.approx(15.0, abs=1e-3)
    assert order["invest"] > 0.0                # never a $0-invest admit
    assert order["sizing_mode"] == "fractional"


def test_fractional_dust_never_emits_zero_invest():
    """Sub-$1 targets (below even the broker floor) are skipped, not emitted."""
    # conv floor can't go that low via multiplier (min target = min_mult×cap);
    # use a tiny PV so the compounded target lands under $1.
    cfg = _config(fractional={"enabled": True}, conv_min_mult=0.01)
    ranked = [_cand("OXY")]
    ctx = _ctx(ranked, ["OXY"], cfg, cash=50.0, pv=60.0,
               prices={"OXY": 100.0})
    SizeAndEmitTask().run(ctx)                   # target 0.15×0.01×60 = $0.09
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["OXY"] == "fractional_dust_skip"  # noqa: SLF001


# ── Admission invariance (§1.3 — HOW MUCH, never WHETHER) ────────────────────


def test_admission_invariance_across_flag_states():
    """The gate/veto/conviction path is identical across every flag state:
    same names blocked by the same NON-size reasons, same names admitted to
    sizing, identical conviction/σ multipliers — only quantity outcomes may
    differ (§6 stage-2 kill condition: any admission delta is a bug class)."""
    tickers = ("OXY", "BLK", "BEARISH")
    prices = {"OXY": 48.0, "BLK": BLK_PRICE, "BEARISH": 100.0}

    def run_mode(config):
        ranked = [_cand("OXY"), _cand("BLK"),
                  _cand("BEARISH", panel_score=-0.10, expected_return=0.03,
                        mu=0.03)]
        ctx = _ctx(ranked, list(tickers), config, prices=prices)
        SizeAndEmitTask().run(ctx)
        return ctx

    modes = {
        "whole":     run_mode(_config()),
        "a3":        run_mode(_config(one_share_floor=True)),
        "frac":      run_mode(_config(fractional={"enabled": True})),
        "frac+a3":   run_mode(_config(fractional={"enabled": True},
                                      one_share_floor=True)),
    }

    base = modes["whole"]
    base_blocked = getattr(base, "_blocked_by_ticker", {})
    # The signal-direction VETO fires identically everywhere.
    assert base_blocked["BEARISH"] == "negative_raw_signal_no_long"
    for name, ctx in modes.items():
        blocked = getattr(ctx, "_blocked_by_ticker", {})
        assert blocked.get("BEARISH") == base_blocked["BEARISH"], name
        # No mode admits a name the gates rejected, or rejects a name the
        # gates admitted, for any NON-size reason.
        for ticker in tickers:
            reason = blocked.get(ticker)
            if reason is not None and ticker != "BEARISH":
                assert reason in ("size_insufficient_cash",
                                  "fractional_dust_skip"), (name, ticker, reason)
        # Conviction path identical: OXY's multipliers match the baseline.
        oxy = next(o for o in ctx.orders if o["ticker"] == "OXY")
        base_oxy = next(o for o in base.orders if o["ticker"] == "OXY")
        assert oxy["conviction"] == base_oxy["conviction"], name
        assert oxy["sigma_mult"] == base_oxy["sigma_mult"], name

    # Quantity outcomes differ EXACTLY as the three arms specify.
    assert "BLK" in getattr(modes["whole"], "_blocked_by_ticker", {})
    assert next(o for o in modes["a3"].orders if o["ticker"] == "BLK")["shares"] == 1
    for name in ("frac", "frac+a3"):
        blk = next(o for o in modes[name].orders if o["ticker"] == "BLK")
        assert blk["shares"] == BLK_FRACTIONAL_QTY, name


# ── A-3 mutual exclusion / supersession + fallback (§7.2) ────────────────────


def test_both_flags_on_fractional_supersedes_one_share_floor():
    """Both flags on ⇒ fractional wins for fractionable names: the A-3
    round-up branch is unreachable (roundups counter stays 0) and the config
    tangle is counted as a warning."""
    ctx = _run(_config(fractional={"enabled": True}, one_share_floor=True))
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["sizing_mode"] == "fractional"
    assert order["shares"] == BLK_FRACTIONAL_QTY
    assert "size_floor_reason" not in order
    assert ctx.counters.get("one_share_floor_roundups", 0) == 0
    assert ctx.counters[
        "config_warning_fractional_supersedes_one_share_floor"] == 1


def test_non_fractionable_symbol_falls_back_to_a3():
    """§7.2 fallback: a non-fractionable symbol keeps the whole-share + A-3
    path even with fractional enabled — A-3 remains the live fallback."""
    cfg = _config(
        fractional={"enabled": True, "non_fractionable_tickers": ["BLK"]},
        one_share_floor=True,
    )
    ctx = _run(cfg)
    assert len(ctx.orders) == 1
    order = ctx.orders[0]
    assert order["shares"] == 1
    assert order["sizing_mode"] == "one_share_floor"
    assert order["size_floor_reason"] == "one_share_floor_round_up"
    assert ctx.counters["one_share_floor_roundups"] == 1


def test_non_fractionable_symbol_without_a3_drops_as_before():
    """Non-fractionable + no A-3 ⇒ the legacy whole-share drop (no new path)."""
    cfg = _config(
        fractional={"enabled": True, "non_fractionable_tickers": ["BLK"]},
    )
    ctx = _run(cfg)
    assert ctx.orders == []
    assert ctx._blocked_by_ticker["BLK"] == "size_insufficient_cash"  # noqa: SLF001


def test_ctx_fractionable_map_false_wins():
    """Broker asset metadata stamped on ctx (fractionable=False) forces the
    whole-share fallback even when the config blocklist is empty."""
    cfg = _config(fractional={"enabled": True}, one_share_floor=True)
    ranked = [_cand("BLK")]
    ctx = _ctx(ranked, ["BLK"], cfg)
    ctx.fractionable_by_ticker = {"BLK": False}
    SizeAndEmitTask().run(ctx)
    assert len(ctx.orders) == 1
    assert ctx.orders[0]["sizing_mode"] == "one_share_floor"
    assert ctx.orders[0]["shares"] == 1


# ── Cheap-name fidelity (the OXY-class 11% undershoot, §1.1) ─────────────────


def test_cheap_name_fractional_removes_floor_undershoot():
    """$48-class name: whole-share floors the target (undershoot); fractional
    realizes it exactly — realized ≤ target always (never rounds up)."""
    whole = _run(_config(), tickers=("OXY",), prices={"OXY": 48.0})
    frac = _run(_config(fractional={"enabled": True}),
                tickers=("OXY",), prices={"OXY": 48.0})
    w, f = whole.orders[0], frac.orders[0]
    assert isinstance(w["shares"], int)
    assert f["shares"] == pytest.approx(TARGET / 48.0, abs=1e-6)
    # Fractional captures the remainder the whole-share floor left behind…
    assert f["invest"] > w["invest"]
    # …but NEVER exceeds the pre-quantization target.
    assert f["invest"] <= f["target_notional"] + 1e-9
    gap = abs(f["realized_notional_planned"] - f["target_notional"]) \
        / f["target_notional"]
    assert gap <= 0.01


# ── Sim parity: pipeline-emitted fractional order round-trips in sim ─────────


def test_sim_parity_fractional_order_round_trip():
    """The order SizeAndEmitTask emits under the flag executes IDENTICALLY on
    the sim path: exact fractional fill, exact cash debit, and a full
    liquidation that leaves ZERO residual (the #153 lifecycle demand, wired
    to the execution-task path this repo owns; the live commit path is the
    stage-0 umbrella contract, RenQuant#439)."""
    from types import SimpleNamespace

    from renquant_pipeline.kernel.execution.backend_sim import SimBackend
    from renquant_pipeline.kernel.exits import ExitSignal
    from renquant_pipeline.kernel.pipeline.task_execution import (
        ExecuteBuysTask,
        ExecuteExitsTask,
        PrepareExecutionTask,
        PruneFullExitsTask,
        UpsertHoldingsTask,
    )

    # 1. Size the BLK order through the REAL selection task.
    sizing_ctx = _run(_config(fractional={"enabled": True}))
    assert len(sizing_ctx.orders) == 1
    order = dict(sizing_ctx.orders[0])
    assert order["shares"] == BLK_FRACTIONAL_QTY

    # 2. Execute it through the execution tasks on a fractional-capable sim.
    be = SimBackend(starting_cash=CASH, allow_fractional=True)
    import pandas as pd
    today = pd.Timestamp("2026-07-02")
    be.update_bar_prices({"BLK": BLK_PRICE}, today)
    ctx = SimpleNamespace(
        execution_backend=be,
        config=_config(fractional={"enabled": True}),
        today=today, fills=[], orders=[order], exits=[],
        holdings={}, last_sell_dates={}, last_stop_exit_dates={},
    )
    PrepareExecutionTask().run(ctx)
    ExecuteBuysTask().run(ctx)
    UpsertHoldingsTask().run(ctx)
    assert be.get_position_quantity("BLK") == pytest.approx(BLK_FRACTIONAL_QTY)
    assert "BLK" in ctx.holdings
    assert ctx.fills[0].shares == pytest.approx(BLK_FRACTIONAL_QTY)

    # 3. Full liquidation reaps the position with zero residual dust.
    ctx.fills = []
    ctx.exits = [("BLK", ExitSignal(should_exit=True, reason="exit",
                                    exit_type="model_sell", quantity=None))]
    ExecuteExitsTask().run(ctx)
    PruneFullExitsTask().run(ctx)
    assert be.get_position_quantity("BLK") == 0.0
    assert "BLK" not in ctx.holdings
    assert ctx.fills[0].shares == pytest.approx(BLK_FRACTIONAL_QTY)


def test_sim_whole_share_backend_fails_fast_on_pipeline_fractional_order():
    """Capability negotiation end-to-end: the pipeline refuses to run a
    fractional config against a whole-share-only backend (fail-fast at
    PrepareExecutionTask — never a silent zero-share fill)."""
    from types import SimpleNamespace

    import pandas as pd

    from renquant_pipeline.kernel.execution.backend_sim import SimBackend
    from renquant_pipeline.kernel.pipeline.task_execution import (
        PrepareExecutionTask,
    )

    be = SimBackend(starting_cash=CASH)          # whole-share only
    be.update_bar_prices({"BLK": BLK_PRICE}, pd.Timestamp("2026-07-02"))
    ctx = SimpleNamespace(
        execution_backend=be,
        config=_config(fractional={"enabled": True}),
        today=pd.Timestamp("2026-07-02"), fills=[], orders=[], exits=[],
        holdings={}, last_sell_dates={}, last_stop_exit_dates={},
    )
    with pytest.raises(ValueError, match="fractional"):
        PrepareExecutionTask().run(ctx)
