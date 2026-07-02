"""S7 lane-B parking sleeve — β-budgeted SPY/SGOV sweep, shadow mode.

Contract: renquant-orchestrator doc/research/2026-07-02-rs1-parking-sleeve.md
(RS-1) + doc/design/2026-07-02-104-capability-program.md §1.3. Pins:

* flag absent / disabled ⇒ the task is byte-inert (ctx unchanged, no file);
* shadow mode computes the β-budget sweep across scenarios (idle cash
  above/below reserve, BEAR fully-off, regime-reserve scaling, several
  w_pos points, sell-first funding) and NEVER places orders;
* the RS-1 §4/§5 monitoring metrics (sleeve contribution, DD-budget
  consumption) are emitted in every record's book_state.
"""
from __future__ import annotations

import copy
import datetime as dt
import inspect
import json
import math

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.task_parking_sleeve import (
    ParkingSleeveShadowTask,
    compute_parking_sleeve_plan,
    load_last_shadow_state,
)


def _ctx(tmp_path, sleeve: dict | None = None, **overrides) -> InferenceContext:
    config = {
        "watchlist": ["AAPL", "OXY"],
        "benchmark": "SPY",
        "_strategy_dir": str(tmp_path),
        "regime_params": {
            "BULL_CALM": {"cash_reserve_pct": 0.0},
            "CHOPPY": {"cash_reserve_pct": 0.30},
            "BEAR": {"cash_reserve_pct": 1.0},
        },
    }
    if sleeve is not None:
        config["sleeve"] = sleeve
    values = {
        "config": config,
        "today": dt.date(2026, 7, 2),
        "regime": "BULL_CALM",
        "confidence": 0.4,  # deliberately low — sleeve reserve must NOT scale with it
        "portfolio_value": 10_000.0,
        "cash": 8_000.0,
        "hwm": 10_000.0,
        "prices": {"SPY": 100.0, "SGOV": 100.0, "AAPL": 200.0, "OXY": 48.0},
    }
    values.update(overrides)
    return InferenceContext(**values)


def _enabled(**extra) -> dict:
    out = {"enabled": True, "mode": "shadow"}
    out.update(extra)
    return out


def _read_log(tmp_path):
    path = tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
    if not path.exists():
        return []
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _actions(rows):
    return [r for r in rows if r["record_type"] == "action"]


def _summaries(rows):
    return [r for r in rows if r["record_type"] == "summary"]


# ── default OFF: byte-inert without the flag ──────────────────────────────────


class TestDefaultOff:

    def test_flag_absent_ctx_byte_identical_and_no_file(self, tmp_path):
        ctx = _ctx(tmp_path)  # no "sleeve" key at all
        ctx.orders.append({"ticker": "OXY", "invest": 336.0, "shares": 7, "price": 48.0})
        baseline = copy.deepcopy(ctx)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx == baseline  # dataclass deep-equality: nothing mutated
        assert not (tmp_path / "logs").exists()

    def test_enabled_false_is_inert(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve={"enabled": False, "mode": "shadow"})
        baseline = copy.deepcopy(ctx)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx == baseline
        assert not (tmp_path / "logs").exists()

    def test_sleeve_config_not_a_dict_is_inert(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.config["sleeve"] = "yes"
        baseline = copy.deepcopy(ctx)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx == baseline

    def test_wired_into_inference_pipeline_after_selection(self):
        from renquant_pipeline.kernel.pipeline import pp_inference

        src = inspect.getsource(pp_inference)
        assert "ParkingSleeveShadowTask" in src
        # after the selection/top-up/trim chain — never competes with admission
        assert src.index("ParkingSleeveShadowTask") > src.index("SelectionJob")
        assert src.index("ParkingSleeveShadowTask") > src.index("TrimHeldTask")


# ── the pure β-budget planner ────────────────────────────────────────────────


class TestBetaBudgetFormula:

    @pytest.mark.parametrize(
        "w_pos_value,expected_frac",
        [
            # sleeve_spy_frac = max(0, (0.6 − w_pos·β_pos) / w_sleeve), capped at 1
            (0.0, None),     # computed below: 0.6 / w_sleeve
            (2_000.0, None),  # (0.6 − 0.2) / w_sleeve
            (4_300.0, None),  # RS-1 §2 mix: (0.6 − 0.43) / w_sleeve
            (7_000.0, 0.0),   # w_pos ≥ β_max ⇒ all SGOV
        ],
    )
    def test_spy_frac_at_several_w_pos(self, w_pos_value, expected_frac):
        pv = 10_000.0
        cash = pv - w_pos_value
        plan = compute_parking_sleeve_plan(
            pv=pv, cash=cash, positions_value=w_pos_value,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        deployable = max(0.0, cash - 0.05 * pv)
        w_sleeve = deployable / pv
        w_pos = w_pos_value / pv
        if expected_frac is None:
            expected_frac = min(max((0.6 - w_pos * 1.0) / w_sleeve, 0.0), 1.0)
        assert plan["sleeve_spy_frac"] == pytest.approx(expected_frac)
        assert plan["target_spy_value"] == pytest.approx(deployable * expected_frac)
        assert plan["target_sgov_value"] == pytest.approx(deployable * (1 - expected_frac))

    def test_beta_pos_override_scales_position_beta(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=6_000.0, positions_value=4_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
            beta_pos=1.2,
        )
        deployable = 6_000.0 - 500.0
        expected = (0.6 - 0.4 * 1.2) / (deployable / 10_000.0)
        assert plan["sleeve_spy_frac"] == pytest.approx(expected)

    def test_sweep_splits_into_spy_and_sgov_whole_shares(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        # deployable 7500; spy_frac (0.6−0.2)/0.75 = 8/15 → SPY 4000, SGOV 3500
        assert plan["reason"] == "sweep_idle_cash"
        assert [(a["action"], a["symbol"], a["qty"]) for a in plan["actions"]] == [
            ("BUY", "SPY", 40), ("BUY", "SGOV", 35),
        ]
        assert sum(a["notional"] for a in plan["actions"]) <= plan["deployable"]

    def test_idle_cash_below_reserve_holds(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=400.0, positions_value=9_600.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        assert plan["deployable"] == 0.0
        assert plan["actions"] == []

    def test_bear_reserve_one_sweeps_sleeve_fully_off(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=500.0, positions_value=2_000.0,
            spy_qty=40.0, spy_price=100.0, sgov_value=3_500.0, sgov_price=100.0,
            regime_cash_reserve_pct=1.0,
        )
        assert plan["reason"] == "bear_regime_sleeve_off"
        assert plan["deployable"] == 0.0
        sells = [a for a in plan["actions"] if a["action"] == "SELL"]
        assert {a["symbol"] for a in sells} == {"SPY", "SGOV"}
        assert sum(a["notional"] for a in sells) == pytest.approx(7_500.0)
        assert all(a["action"] == "SELL" for a in plan["actions"])

    def test_regime_reserve_scales_sleeve_down(self):
        choppy = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
            regime_cash_reserve_pct=0.30,
        )
        calm = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
            regime_cash_reserve_pct=0.0,
        )
        assert choppy["deployable"] == pytest.approx(calm["deployable"] - 3_000.0)
        assert choppy["reason"] == "regime_reserve_scaled_sweep"

    def test_sell_first_funds_admitted_buys(self):
        # Sleeve holds 7500; an admitted single-name buy needs 6000 while
        # only 500 cash sits outside the sleeve ⇒ sleeve sells FIRST.
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=500.0, positions_value=2_000.0,
            spy_qty=40.0, spy_price=100.0, sgov_value=3_500.0, sgov_price=100.0,
            pending_buy_notional=6_000.0,
        )
        assert plan["reason"] == "sell_first_fund_admitted_buys"
        assert plan["funding_shortfall"] == pytest.approx(6_000.0)
        kinds = [a["action"] for a in plan["actions"]]
        assert kinds and kinds == sorted(kinds, key=lambda k: 0 if k == "SELL" else 1)
        sells = [a for a in plan["actions"] if a["action"] == "SELL"]
        assert sum(a["notional"] for a in sells) >= plan["funding_shortfall"] - 1e-6

    def test_open_order_headroom_reduces_sweep(self):
        with_pending = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
            pending_buy_notional=1_000.0,
        )
        without = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        assert with_pending["deployable"] == pytest.approx(without["deployable"] - 1_000.0)

    def test_dust_rebalance_below_min_trade_holds(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=530.0, positions_value=2_000.0,
            spy_qty=40.0, spy_price=100.0, sgov_value=3_500.0, sgov_price=100.0,
        )
        # targets within $50 of current legs → no churn
        assert plan["actions"] == []

    def test_missing_sgov_price_still_plans_notional(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=None,
        )
        sgov = [a for a in plan["actions"] if a["symbol"] == "SGOV"]
        assert len(sgov) == 1
        assert sgov[0]["qty"] is None
        assert sgov[0]["notional"] == pytest.approx(3_500.0)

    def test_invalid_pv_yields_no_actions(self):
        plan = compute_parking_sleeve_plan(
            pv=0.0, cash=1_000.0, positions_value=0.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        assert plan["actions"] == []
        assert plan["reason"] == "invalid_pv"


# ── shadow task: logging, persistence, metrics, no-orders invariant ──────────


class TestShadowTask:

    def test_shadow_places_nothing_and_logs_schema(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled())
        ctx.orders.append({"ticker": "OXY", "invest": 336.0, "shares": 7, "price": 48.0})
        orders_before = copy.deepcopy(ctx.orders)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == orders_before  # NOTHING placed
        assert ctx.exits == []
        rows = _read_log(tmp_path)
        assert rows, "shadow log must be written"
        for row in rows:
            assert {"date", "action", "symbol", "qty", "notional",
                    "reason", "book_state"} <= set(row)
            assert row["date"] == "2026-07-02"
            assert row["book_state"]["live_orders_placed"] is False
        actions = _actions(rows)
        assert {a["action"] for a in actions} == {"BUY"}
        assert {a["symbol"] for a in actions} == {"SPY", "SGOV"}
        assert ctx.counters["parking_sleeve_intended_actions"] == 2

    def test_reserve_not_scaled_by_confidence(self, tmp_path):
        # confidence=0.4 in the fixture; BEAR must still be FULLY off.
        ctx = _ctx(tmp_path, sleeve=_enabled(), regime="BEAR")

        ParkingSleeveShadowTask().run(ctx)

        rows = _read_log(tmp_path)
        assert _summaries(rows)[-1]["book_state"]["regime_cash_reserve_pct"] == 1.0
        assert _summaries(rows)[-1]["book_state"]["deployable"] == 0.0
        assert _actions(rows) == []  # nothing held in shadow yet → plain hold

    def test_metrics_emitted_and_contribution_marks_to_market(self, tmp_path):
        # Session 1: sweep 40 SPY @100 + $3500 SGOV.
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        s1 = _summaries(_read_log(tmp_path))[-1]
        assert s1["book_state"]["sleeve_contribution_abs"] == pytest.approx(0.0)
        assert s1["book_state"]["dd_budget_consumption_pct"] == pytest.approx(0.0)

        # Session 2: SPY 100 → 110, book in a 3% drawdown.
        ctx2 = _ctx(
            tmp_path, sleeve=_enabled(),
            today=dt.date(2026, 7, 3),
            portfolio_value=9_700.0, hwm=10_000.0,
            prices={"SPY": 110.0, "SGOV": 100.0},
        )
        ParkingSleeveShadowTask().run(ctx2)
        s2 = _summaries(_read_log(tmp_path))[-1]
        bs = s2["book_state"]
        # 40 SPY × +$10 = +$400 mark-to-market, invariant under rebalance fills
        assert bs["sleeve_contribution_abs"] == pytest.approx(400.0, abs=1e-6)
        assert bs["drawdown_pct"] == pytest.approx(0.03)
        assert bs["dd_budget_consumption_pct"] == pytest.approx(0.03 / 0.15)
        assert bs["max_dd_budget_consumption_pct"] == pytest.approx(0.03 / 0.15)

    def test_shadow_book_persists_across_sessions(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        state = load_last_shadow_state(
            tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
        )
        assert state["spy_qty"] == 40.0
        assert state["sgov_value"] == pytest.approx(3_500.0)
        assert state["net_invested"] == pytest.approx(7_500.0)

        # Same book next session ⇒ sleeve already at target ⇒ hold.
        ctx2 = _ctx(tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3))
        ParkingSleeveShadowTask().run(ctx2)
        rows = _read_log(tmp_path)
        assert _summaries(rows)[-1]["action"] == "hold"

    def test_sell_first_funding_in_shadow(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)

        # Next session the real pipeline admits a $6000 buy; shadow cash
        # (8000 − 7500 swept) can't fund it ⇒ intended sleeve sells FIRST.
        ctx2 = _ctx(tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3))
        ctx2.orders.append({"ticker": "AAPL", "invest": 6_000.0, "shares": 30, "price": 200.0})
        ParkingSleeveShadowTask().run(ctx2)

        rows = [r for r in _read_log(tmp_path) if r["date"] == "2026-07-03"]
        sells = [r for r in _actions(rows) if r["action"] == "SELL"]
        assert sells, "sleeve must sell first to fund the admitted buy"
        assert all(r["reason"] == "sell_first_fund_admitted_buys" for r in sells)
        assert sum(r["notional"] for r in sells) >= 6_000.0 - 1e-6
        assert ctx2.orders[-1]["ticker"] == "AAPL"  # real order untouched
        assert len(ctx2.orders) == 1

    def test_bear_sweeps_shadow_sleeve_off(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)

        ctx2 = _ctx(tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3), regime="BEAR")
        ParkingSleeveShadowTask().run(ctx2)

        rows = [r for r in _read_log(tmp_path) if r["date"] == "2026-07-03"]
        sells = [r for r in _actions(rows) if r["action"] == "SELL"]
        assert {r["symbol"] for r in sells} == {"SPY", "SGOV"}
        assert all(r["reason"] == "bear_regime_sleeve_off" for r in sells)
        state = load_last_shadow_state(tmp_path / "logs" / "parking_sleeve_shadow.jsonl")
        assert state["spy_qty"] == 0.0
        assert state["sgov_value"] == 0.0

    def test_live_mode_is_unimplemented_and_places_nothing(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled(mode="live"))

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert ctx.exits == []
        assert ctx.counters["parking_sleeve_live_mode_unimplemented"] == 1
        rows = _read_log(tmp_path)
        assert rows and all(r["book_state"]["live_orders_placed"] is False for r in rows)

    def test_task_failure_never_breaks_pipeline(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled(log_path=str(tmp_path)))  # path is a dir → open() fails

        ParkingSleeveShadowTask().run(ctx)  # must not raise

        assert ctx.counters.get("parking_sleeve_error") == 1

    def test_notional_conservation_never_exceeds_deployable(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx)
        rows = _read_log(tmp_path)
        bs = _summaries(rows)[-1]["book_state"]
        buys = sum(r["notional"] for r in _actions(rows) if r["action"] == "BUY")
        assert buys <= bs["deployable"] + 1e-6
        assert math.isclose(
            bs["net_invested"], buys, rel_tol=0, abs_tol=1e-6,
        )
