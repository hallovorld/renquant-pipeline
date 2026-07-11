"""S7 lane-B parking sleeve — β-budgeted SPY/SGOV sweep, shadow + live modes.

Contract: renquant-orchestrator doc/research/2026-07-02-rs1-parking-sleeve.md
(RS-1) + doc/design/2026-07-02-104-capability-program.md §1.3. Pins:

* flag absent / disabled ⇒ the task is byte-inert (ctx unchanged, no file);
* shadow mode computes the β-budget sweep across scenarios (idle cash
  above/below reserve, BEAR fully-off, regime-reserve scaling, several
  w_pos points, sell-first funding) and NEVER places orders — byte-identical
  to the original #157 behavior even when live-era config keys
  (max_sleeve_pct) are present;
* live mode (this change) emits REAL SGOV-floor intents only: SGOV buys of
  idle cash above the reserves, SGOV sells FIRST whenever cash is needed
  (free-before-need), cumulative max_sleeve_pct cap against the real broker
  holding (strategy-104 #44 semantics), §1091 wash-sale engine on re-buys,
  fail-closed when the SGOV price is missing, and NEVER a SPY order;
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
from renquant_pipeline.kernel.exits import HoldingState
from renquant_pipeline.kernel.pipeline.task_parking_sleeve import (
    ECONOMIC_BOOK_STATE_FIELDS,
    OPERATIONAL_BOOK_STATE_FIELDS,
    ParkingSleeveShadowTask,
    build_economic_scorecard,
    build_operational_scorecard,
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


def _live(**extra) -> dict:
    out = {"enabled": True, "mode": "live"}
    out.update(extra)
    return out


def _sgov_holding(shares: float, entry_price: float = 100.0) -> HoldingState:
    return HoldingState(
        entry_price=entry_price,
        entry_date=dt.date(2026, 6, 1),
        high_watermark=entry_price,
        shares=float(shares),
    )


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

    def test_unknown_mode_treated_as_shadow_places_nothing(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled(mode="paper"))

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert ctx.exits == []
        assert ctx.counters["parking_sleeve_bad_mode"] == 1
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


# ── idempotency / concurrency guard ───────────────────────────────────────


class TestIdempotency:

    def test_rerun_same_date_is_skipped_not_double_applied(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        rows_after_first = _read_log(tmp_path)
        state_after_first = load_last_shadow_state(
            tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
        )

        # Same ctx, same date (a retry after a transient failure, or a
        # second concurrent run) — must not append a second set of rows or
        # roll the shadow book forward a second time.
        ctx2 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx2)

        rows_after_second = _read_log(tmp_path)
        assert rows_after_second == rows_after_first, \
            "duplicate run for an already-logged date must not append anything"
        assert ctx2.counters["parking_sleeve_duplicate_date_skipped"] == 1
        state_after_second = load_last_shadow_state(
            tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
        )
        assert state_after_second == state_after_first, \
            "shadow book must not be double-applied for a re-run of the same date"

    def test_scorecard_reports_zero_duplicate_dates_when_guard_holds(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        ctx2 = _ctx(tmp_path, sleeve=_enabled())  # same date — should be a no-op skip
        ParkingSleeveShadowTask().run(ctx2)
        ctx3 = _ctx(tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3))
        ParkingSleeveShadowTask().run(ctx3)

        rows = _read_log(tmp_path)
        scorecard = build_operational_scorecard(rows)
        assert scorecard["duplicate_summary_dates"] == 0
        assert scorecard["schema_complete"] is True


# ── SGOV valuation semantics (cost, no carry) ─────────────────────────────


class TestSgovValuationSemantics:

    def test_sgov_price_appreciation_alone_does_not_move_sleeve_value(self, tmp_path):
        # Session 1: sweep into SPY + SGOV.
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        state1 = load_last_shadow_state(
            tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
        )
        assert state1["sgov_value"] > 0

        # Session 2: SGOV price "appreciates" (simulating real T-bill NAV
        # accretion) but the book is already at target ⇒ no new buy/sell.
        # SGOV is tracked at cost (module contract) so its persisted value
        # must be byte-identical to session 1 despite the price move —
        # unlike SPY, which IS marked to market.
        ctx2 = _ctx(
            tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3),
            prices={"SPY": 100.0, "SGOV": 100.50},
        )
        ParkingSleeveShadowTask().run(ctx2)
        state2 = load_last_shadow_state(
            tmp_path / "logs" / "parking_sleeve_shadow.jsonl"
        )
        assert state2["sgov_value"] == pytest.approx(state1["sgov_value"]), \
            "SGOV is cost-basis only — a price move alone must not change its value"

        summary = _summaries(_read_log(tmp_path))[-1]
        assert summary["book_state"]["sgov_valuation_mode"] == "cost_no_carry"

    def test_sgov_valuation_mode_stamped_on_every_row(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx)
        rows = _read_log(tmp_path)
        assert rows
        assert all(r["book_state"]["sgov_valuation_mode"] == "cost_no_carry" for r in rows)


# ── operational vs economic scorecard separation ──────────────────────────


class TestScorecardSeparation:

    def test_scorecards_do_not_share_fields(self):
        assert OPERATIONAL_BOOK_STATE_FIELDS.isdisjoint(ECONOMIC_BOOK_STATE_FIELDS)

    def test_operational_scorecard_never_reports_economic_merit(self, tmp_path):
        ctx1 = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx1)
        ctx2 = _ctx(
            tmp_path, sleeve=_enabled(), today=dt.date(2026, 7, 3),
            portfolio_value=9_700.0, hwm=10_000.0,
            prices={"SPY": 110.0, "SGOV": 100.0},
        )
        ParkingSleeveShadowTask().run(ctx2)

        rows = _read_log(tmp_path)
        operational = build_operational_scorecard(rows)
        economic = build_economic_scorecard(rows)

        # No economic-merit field name leaks into the operational scorecard.
        assert not (set(operational) & ECONOMIC_BOOK_STATE_FIELDS)
        assert "sleeve_contribution_abs" not in operational
        assert "drawdown_pct" not in operational
        # The operational scorecard is pure hygiene — never an authorization
        # signal by itself.
        assert "authorization_grade" not in operational

    def test_economic_scorecard_is_explicitly_not_authorization_grade(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx)
        rows = _read_log(tmp_path)
        economic = build_economic_scorecard(rows)
        assert economic["authorization_grade"] is False
        assert economic["n_sessions"] == 1
        assert "final_sleeve_contribution_pct" in economic

    def test_economic_scorecard_empty_log_is_not_authorization_grade(self):
        assert build_economic_scorecard([]) == {"authorization_grade": False, "n_sessions": 0}


# ── planner: live-era parameters (cap + SGOV-only) ─────────────────────────


class TestPlannerLiveParams:

    def test_max_sleeve_cap_binds_target(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=None, sgov_value=0.0, sgov_price=100.0,
            max_sleeve_pct=0.50, sgov_only=True,
        )
        # deployable 7500 pre-cap → capped at 0.5·PV = 5000
        assert plan["deployable"] == pytest.approx(5_000.0)
        assert plan["sleeve_cap_bound"] is True
        assert plan["reason"] == "max_sleeve_cap_enforced"
        assert plan["target_sgov_value"] == pytest.approx(5_000.0)

    def test_default_cap_of_one_never_binds(self):
        capped = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
        )
        assert capped["sleeve_cap_bound"] is False
        assert capped["reason"] == "sweep_idle_cash"  # unchanged vs #157

    def test_sgov_only_forces_zero_spy_frac(self):
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=8_000.0, positions_value=2_000.0,
            spy_qty=0.0, spy_price=100.0, sgov_value=0.0, sgov_price=100.0,
            sgov_only=True,
        )
        assert plan["sleeve_spy_frac"] == 0.0
        assert plan["target_spy_value"] == 0.0
        assert all(a["symbol"] == "SGOV" for a in plan["actions"])

    def test_cumulative_cap_over_cap_plans_sell_down(self):
        # Sleeve already 60% of PV with a 50% cap ⇒ plan a $1000 sell-down.
        plan = compute_parking_sleeve_plan(
            pv=10_000.0, cash=500.0, positions_value=3_500.0,
            spy_qty=0.0, spy_price=None, sgov_value=6_000.0, sgov_price=100.0,
            max_sleeve_pct=0.50, sgov_only=True,
        )
        assert plan["deployable"] == pytest.approx(5_000.0)
        sells = [a for a in plan["actions"] if a["action"] == "SELL"]
        assert len(sells) == 1 and sells[0]["symbol"] == "SGOV"
        assert sells[0]["notional"] == pytest.approx(1_000.0)


# ── live mode: real SGOV-floor emission ─────────────────────────────────────


def _live_ctx(tmp_path, **overrides):
    return _ctx(tmp_path, sleeve=_live(), **overrides)


class TestLiveMode:

    def test_live_buy_emits_whole_share_sgov_order_and_never_spy(self, tmp_path):
        ctx = _live_ctx(tmp_path)  # pv 10k, cash 8k, SGOV 100

        ParkingSleeveShadowTask().run(ctx)

        assert len(ctx.orders) == 1
        order = ctx.orders[0]
        assert order["ticker"] == "SGOV"
        assert order["order_type"] == "PARKING_SLEEVE_BUY"
        # deployable 7500 → cumulative cap (default 0.50) → 5000; whole
        # shares at 100 with the 3bps buy-cost multiplier ⇒ 49 shares.
        assert order["shares"] == 49.0
        assert order["invest"] == pytest.approx(4_900.0)
        assert order["source_job"] == "ParkingSleeveJob"
        # SPY arm is dark: never a SPY order or exit from the live sleeve.
        assert all(o["ticker"] != "SPY" for o in ctx.orders)
        assert ctx.exits == []
        assert ctx.counters["parking_sleeve_live_orders"] == 1

    def test_live_buy_leaves_reserve_and_pending_funded(self, tmp_path):
        ctx = _live_ctx(tmp_path)
        ctx.orders.append({"ticker": "AAPL", "invest": 1_000.0, "shares": 5, "price": 200.0})

        ParkingSleeveShadowTask().run(ctx)

        sleeve_buys = [o for o in ctx.orders if o["ticker"] == "SGOV"]
        assert len(sleeve_buys) == 1
        invest = sleeve_buys[0]["invest"]
        pending = 1_000.0
        reserve = 0.05 * 10_000.0
        # CRITICAL liquidity invariant: the sleeve buy never digs into the
        # pending main-strategy buys or the operational reserve.
        assert ctx.cash - pending - invest >= reserve - 1e-9

    def test_live_sell_first_frees_cash_before_admitted_buys_need_it(self, tmp_path):
        ctx = _live_ctx(tmp_path, cash=500.0)
        ctx.holdings["SGOV"] = _sgov_holding(75.0)
        ctx.orders.append({"ticker": "AAPL", "invest": 6_000.0, "shares": 30, "price": 200.0})

        ParkingSleeveShadowTask().run(ctx)

        # A $6000 admitted buy with $500 cash ⇒ the sleeve SELLS first.
        assert len(ctx.exits) == 1
        ticker, sig = ctx.exits[0]
        assert ticker == "SGOV"
        assert sig.exit_type == "parking_sleeve_sweep"
        assert sig.quantity == 61.0  # ceil(6000 / (100 · sell-proceeds mult))
        # Net proceeds (after friction) cover the full funding shortfall.
        from renquant_pipeline.kernel.pipeline.task_benchmark_sleeve import (
            _sell_proceeds_multiplier,
        )
        freed = sig.quantity * 100.0 * _sell_proceeds_multiplier(ctx.config)
        assert freed >= 6_000.0 - 1e-6
        # No sleeve buy in the same bar; the main order is untouched.
        assert [o["ticker"] for o in ctx.orders] == ["AAPL"]
        # Free-before-need at execution: exits are executed before buys.
        from renquant_pipeline.kernel.pipeline.pp_execution import (
            BuysJob, ExecutionPipeline, ExitsJob,
        )
        jobs = ExecutionPipeline().jobs
        assert isinstance(jobs[0], ExitsJob) and isinstance(jobs[1], BuysJob)

    def test_live_cumulative_cap_counts_real_holding_across_sessions(self, tmp_path):
        # 45 SGOV shares already held (45% of PV) — new deployment must be
        # capped at the 5% headroom below the 50% cumulative cap.
        ctx = _live_ctx(tmp_path, cash=3_000.0)
        ctx.holdings["SGOV"] = _sgov_holding(45.0)

        ParkingSleeveShadowTask().run(ctx)

        sleeve_buys = [o for o in ctx.orders if o["ticker"] == "SGOV"]
        assert len(sleeve_buys) == 1
        assert sleeve_buys[0]["shares"] == 4.0  # $500 headroom ⇒ 4 whole shares
        assert 45.0 * 100.0 + sleeve_buys[0]["invest"] <= 0.50 * 10_000.0 + 1e-9

    def test_live_over_cap_sleeve_sells_down_to_cap(self, tmp_path):
        ctx = _live_ctx(tmp_path, cash=500.0)
        ctx.holdings["SGOV"] = _sgov_holding(60.0)  # 60% of PV > 50% cap

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert len(ctx.exits) == 1
        _ticker, sig = ctx.exits[0]
        assert sig.quantity == 11.0  # ≈ $1000 over cap, ceil'd for friction
        rows = _read_log(tmp_path)
        assert _summaries(rows)[-1]["reason"] == "max_sleeve_cap_enforced"

    def test_live_missing_sgov_price_fail_closed_no_buys(self, tmp_path):
        ctx = _live_ctx(tmp_path, prices={"SPY": 100.0, "AAPL": 200.0, "OXY": 48.0})

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert ctx.exits == []
        assert ctx.counters["parking_sleeve_live_missing_sgov_price"] == 1
        rows = _read_log(tmp_path)
        assert rows
        summary = _summaries(rows)[-1]
        assert summary["book_state"]["live_orders_placed"] is False
        assert "sgov_price_missing_live_fail_closed" in summary["book_state"]["blocked"]
        assert summary["reason"] == "sgov_price_missing_fail_closed"

    def test_live_missing_price_with_shortfall_still_frees_cash_via_full_exit(self, tmp_path):
        ctx = _live_ctx(
            tmp_path, cash=100.0,
            prices={"SPY": 100.0, "AAPL": 200.0, "OXY": 48.0},
        )
        ctx.holdings["SGOV"] = _sgov_holding(75.0)
        ctx.orders.append({"ticker": "AAPL", "invest": 6_000.0, "shares": 30, "price": 200.0})

        ParkingSleeveShadowTask().run(ctx)

        # Fail-closed still honors free-before-need: a FULL exit (no price
        # needed) frees the position when the book needs the cash.
        assert len(ctx.exits) == 1
        ticker, sig = ctx.exits[0]
        assert ticker == "SGOV"
        assert sig.quantity is None  # full liquidation
        assert [o["ticker"] for o in ctx.orders] == ["AAPL"]  # no sleeve buys
        assert ctx.counters["parking_sleeve_live_missing_sgov_price"] == 1
        rows = _read_log(tmp_path)
        assert _summaries(rows)[-1]["reason"] == "sgov_price_missing_fail_closed_full_exit"

    def test_live_wash_sale_blocks_rebuy_after_recent_loss_sale(self, tmp_path):
        ctx = _live_ctx(tmp_path)
        ctx.last_sell_dates["SGOV"] = dt.date(2026, 6, 28)  # 4d ago
        ctx.last_sell_pls["SGOV"] = -12.0  # LOSS ⇒ §1091 applies

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert ctx.counters["parking_sleeve_live_wash_sale_blocked"] == 1
        rows = _read_log(tmp_path)
        assert "sgov_wash_sale_blocked" in _summaries(rows)[-1]["book_state"]["blocked"]

    def test_live_wash_sale_gain_sale_passes(self, tmp_path):
        ctx = _live_ctx(tmp_path)
        ctx.last_sell_dates["SGOV"] = dt.date(2026, 6, 28)
        ctx.last_sell_pls["SGOV"] = +12.0  # GAIN ⇒ §1091 does not apply

        ParkingSleeveShadowTask().run(ctx)

        assert len(ctx.orders) == 1 and ctx.orders[0]["ticker"] == "SGOV"

    def test_live_buy_gates_block_buys(self, tmp_path):
        ctx = _live_ctx(tmp_path, skip_buys=True)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert ctx.counters["parking_sleeve_live_buy_gated"] == 1
        rows = _read_log(tmp_path)
        assert "live_buy_gates_blocked" in _summaries(rows)[-1]["book_state"]["blocked"]

    def test_live_bear_sweeps_real_sleeve_off_even_with_buys_gated(self, tmp_path):
        # Exits-always-allowed: sells fire in BEAR regardless of buy gates.
        ctx = _live_ctx(tmp_path, cash=500.0, regime="BEAR", skip_buys=True)
        ctx.holdings["SGOV"] = _sgov_holding(75.0)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == []
        assert len(ctx.exits) == 1
        _ticker, sig = ctx.exits[0]
        assert sig.quantity is None  # full liquidation
        rows = _read_log(tmp_path)
        assert _summaries(rows)[-1]["reason"] == "bear_regime_sleeve_off"

    def test_live_symbol_already_touched_stands_down(self, tmp_path):
        ctx = _live_ctx(tmp_path)
        ctx.orders.append({"ticker": "SGOV", "invest": 100.0, "shares": 1, "price": 100.0})

        ParkingSleeveShadowTask().run(ctx)

        assert len(ctx.orders) == 1  # nothing stacked on top
        assert ctx.exits == []
        assert ctx.counters["parking_sleeve_live_symbol_already_touched"] == 1

    def test_live_idempotency_same_date_never_reemits(self, tmp_path):
        ctx1 = _live_ctx(tmp_path)
        ParkingSleeveShadowTask().run(ctx1)
        assert len(ctx1.orders) == 1

        ctx2 = _live_ctx(tmp_path)  # same date, fresh ctx (a retry)
        ParkingSleeveShadowTask().run(ctx2)

        assert ctx2.orders == []
        assert ctx2.exits == []
        assert ctx2.counters["parking_sleeve_duplicate_date_skipped"] == 1

    def test_live_rows_logged_with_live_mode_and_real_book_state(self, tmp_path):
        ctx = _live_ctx(tmp_path)
        ParkingSleeveShadowTask().run(ctx)

        rows = _read_log(tmp_path)
        assert rows
        for row in rows:
            assert {"date", "action", "symbol", "qty", "notional",
                    "reason", "book_state"} <= set(row)
            assert row["book_state"]["mode"] == "live"
            assert row["book_state"]["live_orders_placed"] is True
            assert row["book_state"]["sgov_valuation_mode"] == "mark_to_market"
        summary = _summaries(rows)[-1]
        # shadow_state mirrors the REAL post-trade book (49 shares at cost).
        assert summary["shadow_state"]["spy_qty"] == 0.0
        assert summary["shadow_state"]["sgov_value"] == pytest.approx(4_900.0)

    def test_live_failure_never_breaks_pipeline(self, tmp_path):
        # log_path pointing at a directory → open() fails inside the task
        ctx = _ctx(tmp_path, sleeve=_live(log_path=str(tmp_path)))

        ParkingSleeveShadowTask().run(ctx)  # must not raise

        assert ctx.counters.get("parking_sleeve_error") == 1


# ── shadow-mode regression: byte-identical to #157 ─────────────────────────


class TestShadowByteIdenticalRegression:

    def test_shadow_ignores_live_era_config_keys(self, tmp_path):
        base_dir = tmp_path / "base"
        keyed_dir = tmp_path / "keyed"
        base_dir.mkdir()
        keyed_dir.mkdir()

        ctx_base = _ctx(base_dir, sleeve=_enabled())
        ParkingSleeveShadowTask().run(ctx_base)
        ctx_keyed = _ctx(keyed_dir, sleeve=_enabled(max_sleeve_pct=0.50))
        ParkingSleeveShadowTask().run(ctx_keyed)

        rows_base = _read_log(base_dir)
        rows_keyed = _read_log(keyed_dir)
        assert rows_base == rows_keyed, \
            "shadow mode must NOT apply max_sleeve_pct (byte-identical #157 corpus)"
        assert ctx_keyed.orders == [] and ctx_keyed.exits == []

    def test_shadow_never_touches_orders_even_with_holdings(self, tmp_path):
        ctx = _ctx(tmp_path, sleeve=_enabled())
        ctx.holdings["SGOV"] = _sgov_holding(10.0)
        orders_before = copy.deepcopy(ctx.orders)

        ParkingSleeveShadowTask().run(ctx)

        assert ctx.orders == orders_before
        assert ctx.exits == []
        # The shadow book still starts empty — real holdings are a LIVE
        # concept; shadow keeps its own JSONL book (per #157).
        rows = _read_log(tmp_path)
        bs = _summaries(rows)[-1]["book_state"]
        assert bs["mode"] == "shadow"
        assert bs["live_orders_placed"] is False
