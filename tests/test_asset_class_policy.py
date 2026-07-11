"""Asset-class execution policy (crypto RFC 2026-07-10, pipeline gaps P1-P7).

One test class per gap plus the P11 switch itself. EVERY class carries an
equity byte-identity pin: an ABSENT ``asset_class`` (or explicit
``"us_equity"``) must reproduce the legacy behavior exactly — the crypto
sleeve may never move an equity decision.

Date fixtures use the plain weekend Fri 2026-06-26 / Sat 06-27 / Sun 06-28
(no NYSE holiday adjacency) unless a holiday is the point of the test.
"""
from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from renquant_pipeline.kernel.asset_class import (
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_US_EQUITY,
    annualization_days_for,
    is_crypto,
    is_validated_crypto_spot_pair,
    last_completed_always_open_session,
    resolve_asset_class,
    resolve_validated_crypto_spot_pairs,
    settlement_days_for,
    sigma_clip_bounds_for,
    wash_sale_applies,
    wash_sale_applies_for_ticker,
)

FRI = dt.date(2026, 6, 26)
SAT = dt.date(2026, 6, 27)
SUN = dt.date(2026, 6, 28)
MON = dt.date(2026, 6, 29)


# ─── P11: the one switch ────────────────────────────────────────────────────

class TestResolveAssetClass:
    def test_absent_defaults_to_us_equity(self):
        assert resolve_asset_class({}) == ASSET_CLASS_US_EQUITY
        assert resolve_asset_class(None) == ASSET_CLASS_US_EQUITY
        assert resolve_asset_class({"watchlist": ["AAPL"]}) == ASSET_CLASS_US_EQUITY

    def test_explicit_crypto(self):
        assert resolve_asset_class({"asset_class": "crypto"}) == ASSET_CLASS_CRYPTO
        assert is_crypto({"asset_class": "crypto"})
        assert not is_crypto({})

    def test_unknown_value_fails_closed(self):
        with pytest.raises(ValueError, match="unknown asset_class"):
            resolve_asset_class({"asset_class": "cryto"})  # typo must not pass
        with pytest.raises(ValueError):
            is_crypto("equities")

    def test_policy_table(self):
        assert annualization_days_for(ASSET_CLASS_US_EQUITY) == 252.0
        assert annualization_days_for(ASSET_CLASS_CRYPTO) == 365.0
        assert settlement_days_for(ASSET_CLASS_US_EQUITY) == 1
        assert settlement_days_for(ASSET_CLASS_US_EQUITY, equity_days=2) == 2
        assert settlement_days_for(ASSET_CLASS_CRYPTO) == 0
        assert settlement_days_for(ASSET_CLASS_CRYPTO, equity_days=2) == 0
        assert wash_sale_applies(ASSET_CLASS_US_EQUITY)
        assert not wash_sale_applies(ASSET_CLASS_CRYPTO)
        assert sigma_clip_bounds_for(ASSET_CLASS_US_EQUITY) == (0.05, 1.50)
        assert sigma_clip_bounds_for(ASSET_CLASS_CRYPTO) == (0.20, 3.00)

    def test_schema_accepts_crypto_and_rejects_typos(self):
        from renquant_pipeline.kernel.config_schema import (
            ConfigSchemaError,
            validate_strategy_config,
        )
        base = {
            "model_name": "m",
            "watchlist": ["BTC/USD"],
            "benchmark": "BTC/USD",
            "wash_sale_days": 0,
            "min_hold_days": 2,
            "max_hold_days": 40,
            "max_concurrent_positions": 3,
            "regime": {
                "bear_vol_threshold": 0.5,
                "bear_return_threshold": -0.1,
                "bear_vol_threshold_5d": 0.5,
                "bear_return_threshold_5d": -0.1,
                "transition_uncertainty_bars": 2,
                "bear_short_route_require_both": True,
            },
        }
        rep = validate_strategy_config(dict(base), mode="strict")
        assert rep.ok and rep.config.asset_class == "us_equity"  # pinned default
        rep = validate_strategy_config(
            {**base, "asset_class": "crypto"}, mode="strict"
        )
        assert rep.ok and rep.config.asset_class == "crypto"
        with pytest.raises(ConfigSchemaError):
            validate_strategy_config({**base, "asset_class": "cryto"}, mode="strict")


# ─── P1: freshness clock ────────────────────────────────────────────────────

def _daily_df(dates):
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
        index=idx,
    )


class TestP1FreshnessCalendar:
    #: Sunday 18:00 UTC — mid-weekend reference instant.
    REF = pd.Timestamp("2026-06-28 18:00:00", tz="UTC")

    def test_last_completed_always_open_session_is_yesterday_utc(self):
        assert last_completed_always_open_session(self.REF) == SAT
        assert last_completed_always_open_session(SUN) == SAT

    def test_delegates_to_shared_calendar_around_utc_midnight(self):
        """Integration pin (Codex re-review of #183): the pipeline result
        EQUALS the canonical shared-calendar primitive for naive-UTC and
        aware-offset instants around UTC midnight — there is no local
        re-implementation that could fork from it."""
        from renquant_common import market_calendar as mc

        probes = [
            # Aware straddle: 2026-06-28T20:30-04:00 == 00:30 UTC June 29.
            pd.Timestamp("2026-06-28T20:30:00-04:00"),
            dt.datetime(2026, 6, 28, 20, 30,
                        tzinfo=dt.timezone(dt.timedelta(hours=-4))),
            # Aware UTC just after midnight.
            pd.Timestamp("2026-06-29 00:00:01", tz="UTC"),
            # Naive instants near UTC midnight (naive == UTC by convention).
            dt.datetime(2026, 6, 28, 20, 59),
            dt.datetime(2026, 6, 28, 21, 59),
            dt.datetime(2026, 6, 28, 22, 59),
            dt.datetime(2026, 6, 28, 23, 59),
            pd.Timestamp("2026-06-28 23:59:00"),
            SUN,
        ]
        for ref in probes:
            assert last_completed_always_open_session(ref) == \
                mc.last_completed_session(ref, calendar_name="ALWAYS_OPEN"), ref
        # Spot-check absolute values so the equality above cannot be
        # trivially satisfied by a shared wrong answer.
        assert last_completed_always_open_session(
            pd.Timestamp("2026-06-28T20:30:00-04:00")
        ) == SUN  # 00:30 UTC June 29 → June 28 completed
        assert last_completed_always_open_session(
            dt.datetime(2026, 6, 28, 23, 59)
        ) == SAT  # naive == UTC: still June 28 → June 27 completed

    def test_fails_closed_when_common_lacks_always_open_mode(self, monkeypatch):
        """No-local-fork pin (Codex re-review of #183): with the common
        capability masked (a pre-0.11.0 renquant-common), the crypto
        calendar path raises a clear error instead of degrading onto a
        forked local clock — and the freshness gate consuming it fails
        closed too. Merge order: common #27 first, then this PR."""
        from renquant_common import market_calendar as mc
        from renquant_pipeline.kernel.pipeline.task_data_freshness import (
            DataFreshnessGateTask,
        )

        # raising=False: on a genuinely old common the attribute is absent —
        # setting it to None models the same missing capability either way.
        monkeypatch.setattr(mc, "ALWAYS_OPEN_CALENDAR_NAME", None, raising=False)
        with pytest.raises(RuntimeError, match="renquant-common >= 0.11.0"):
            last_completed_always_open_session(self.REF)
        # The consuming gate propagates the fail-closed error (no silent
        # tolerance-path degradation) even with FRESH data present.
        cfg = {
            "asset_class": "crypto",
            "data_freshness": {"require_expected_symbols": False},
        }
        with pytest.raises(RuntimeError, match="ALWAYS_OPEN"):
            DataFreshnessGateTask().run(self._gate_ctx(cfg, SAT))
        # Equity path is untouched by the mask.
        eq_cfg = {"data_freshness": {"require_expected_symbols": False}}
        assert DataFreshnessGateTask().run(self._gate_ctx(eq_cfg, FRI)) is True

    def test_local_store_crypto_requires_saturday_bar_on_sunday(self, tmp_path):
        from renquant_pipeline.kernel.data import LocalStore

        store = LocalStore(data_dir=tmp_path)
        store.save(_daily_df([FRI]), "BTCUSD")
        # Equity clock: Friday bar is fresh on Sunday (last NYSE close = Fri).
        assert store.has_range("BTCUSD", end=self.REF) is True
        assert store.has_range("BTCUSD", end=self.REF, asset_class="us_equity") is True
        # Crypto clock: Saturday's UTC bar is required — Friday-only is STALE.
        assert store.has_range("BTCUSD", end=self.REF, asset_class="crypto") is False
        store.save(_daily_df([FRI, SAT]), "BTCUSD")
        assert store.has_range("BTCUSD", end=self.REF, asset_class="crypto") is True

    def _gate_ctx(self, config, max_date):
        return SimpleNamespace(
            config=config,
            today=SUN,
            run_timestamp=self.REF,
            ohlcv={"BTC/USD": _daily_df(pd.date_range("2026-06-20", max_date))},
            holdings={},
        )

    def test_freshness_gate_crypto_stale_without_weekend_bar(self):
        from renquant_pipeline.kernel.pipeline.task_data_freshness import (
            DataFreshnessGateTask,
        )
        cfg = {
            "asset_class": "crypto",
            "data_freshness": {"require_expected_symbols": False},
        }
        # Saturday bar present → PASS.
        assert DataFreshnessGateTask().run(self._gate_ctx(cfg, SAT)) is True
        # Friday-only on Sunday → STALE (fail-closed).
        with pytest.raises(RuntimeError, match="UTC-day"):
            DataFreshnessGateTask().run(self._gate_ctx(cfg, FRI))

    def test_freshness_gate_equity_unchanged_on_weekend(self):
        from renquant_pipeline.kernel.pipeline.task_data_freshness import (
            DataFreshnessGateTask,
        )
        cfg = {"data_freshness": {"require_expected_symbols": False}}
        # Equity pin: Friday data on Sunday is fresh — absent asset_class
        # must not tighten the equity clock.
        assert DataFreshnessGateTask().run(self._gate_ctx(cfg, FRI)) is True

    def test_typed_gate_crypto_and_equity(self):
        from renquant_pipeline.kernel.typed_past.typed_data_freshness import (
            TypedDataFreshnessGate,
        )
        past_fri = SimpleNamespace(ohlcv=_daily_df(pd.date_range("2026-06-20", FRI)))
        past_sat = SimpleNamespace(ohlcv=_daily_df(pd.date_range("2026-06-20", SAT)))
        t = pd.Timestamp(SUN)
        # Equity pin: default construction unchanged, Friday passes on Sunday.
        res = TypedDataFreshnessGate().values_in_time(t, past_fri)
        assert res.continue_chain
        gate = TypedDataFreshnessGate(asset_class="crypto")
        assert gate.values_in_time(t, past_sat).continue_chain
        with pytest.raises(RuntimeError, match="UTC-day"):
            gate.values_in_time(t, past_fri)
        with pytest.raises(ValueError):
            TypedDataFreshnessGate(asset_class="nope")


# ─── P2: hold/streak clocks ─────────────────────────────────────────────────

class TestP2HoldClocks:
    def test_trading_days_between_dispatch(self):
        from renquant_pipeline.kernel.exits import (
            nyse_trading_days_between,
            trading_days_between,
        )
        # Equity pin: default == the NYSE implementation, holiday-aware
        # (2026-07-03 is the July-4 observance — closed).
        span = (dt.date(2026, 7, 1), dt.date(2026, 7, 6))
        assert trading_days_between(*span) == nyse_trading_days_between(*span) == 2
        # Crypto: calendar days — a position ages over the weekend.
        assert trading_days_between(FRI, MON, asset_class="crypto") == 3
        assert trading_days_between(FRI, MON) == 1

    def test_is_trading_day_dispatch(self):
        from renquant_pipeline.kernel.exits import is_trading_day
        assert is_trading_day(SUN, asset_class="crypto") is True
        assert is_trading_day(SUN) is False

    def test_streak_advances_on_sunday_for_crypto_only(self):
        from renquant_pipeline.kernel.exits import HoldingState, check_model_sell
        def _hs():
            return HoldingState(
                entry_price=100.0, entry_date=dt.date(2026, 6, 22),
                high_watermark=100.0,
            )
        # Equity pin: Sunday never moves nor fires the streak.
        st, sig = check_model_sell("sell", _hs(), 1, 0, SUN)
        assert st.sell_streak == 0 and not sig.should_exit
        # Crypto: Sunday IS a trading day — streak increments and fires.
        st, sig = check_model_sell("sell", _hs(), 1, 0, SUN, asset_class="crypto")
        assert st.sell_streak == 1 and sig.should_exit

    def test_min_hold_counts_calendar_days_for_crypto(self):
        from renquant_pipeline.kernel.exits import HoldingState, check_model_sell
        def _hs():
            return HoldingState(
                entry_price=100.0, entry_date=FRI, high_watermark=100.0,
            )
        # Fri→Sun: equity 0 NYSE days < 2 → blocked; crypto 2 calendar ≥ 2.
        st, sig = check_model_sell("sell", _hs(), 1, 2, SUN)
        assert st.sell_streak == 0 and not sig.should_exit
        st, sig = check_model_sell("sell", _hs(), 1, 2, SUN, asset_class="crypto")
        assert sig.should_exit

    def test_soft_exit_horizon_ages_over_weekend_for_crypto(self):
        from renquant_pipeline.kernel.pipeline.soft_exit_guards import (
            soft_exit_horizon_suppression,
            trading_holding_days,
        )
        holding = SimpleNamespace(entry_date=dt.date(2026, 6, 25))  # Thursday
        assert trading_holding_days(SUN, holding) == 1               # equity pin
        assert trading_holding_days(SUN, holding, asset_class="crypto") == 3
        cfg = {"min_holding_days": 3}
        suppressed, _ = soft_exit_horizon_suppression(
            panel_cfg=cfg, regime=None, today=SUN, holding=holding,
        )
        assert suppressed  # equity: 1 trading day < 3
        suppressed, _ = soft_exit_horizon_suppression(
            panel_cfg=cfg, regime=None, today=SUN, holding=holding,
            asset_class="crypto",
        )
        assert not suppressed  # crypto: 3 calendar days ≥ 3


# ─── P3: settlement ─────────────────────────────────────────────────────────

class TestP3Settlement:
    def test_crypto_settles_instantly(self):
        from renquant_pipeline.kernel.execution.t2_settlement import T2CashQueue
        q = T2CashQueue.for_asset_class("crypto")
        assert q.settlement_days == 0
        q.add_pending(pd.Timestamp(SAT), 100.0)   # Saturday sale
        assert q.drain(pd.Timestamp(SAT)) == 100.0  # same-day cash

    def test_equity_t1_unchanged(self):
        from renquant_pipeline.kernel.execution.t2_settlement import T2CashQueue
        q = T2CashQueue.for_asset_class("us_equity")
        assert q.settlement_days == 1  # byte-identical default
        q.add_pending(pd.Timestamp(FRI), 100.0)
        assert q.drain(pd.Timestamp(FRI)) == 0.0
        assert q.drain(pd.Timestamp(SUN)) == 0.0    # weekend: not settled
        assert q.drain(pd.Timestamp(MON)) == 100.0  # next NYSE session

    def test_sim_backend_bypasses_queue_for_crypto(self):
        from renquant_pipeline.kernel.execution.backend_sim import SimBackend
        equity = SimBackend(exec_enabled=True, t2_days=1)
        assert equity._t2_queue is not None
        assert equity._t2_queue.settlement_days == 1
        crypto = SimBackend(exec_enabled=True, t2_days=1, asset_class="crypto")
        assert crypto._t2_queue is None  # T+0: queue structurally bypassed


# ─── P4: annualization ──────────────────────────────────────────────────────

class TestP4Annualization:
    def test_vol_target_annualization(self):
        from renquant_pipeline.kernel.vol_target import compute_vol_target_scale
        rets = [0.01, -0.01] * 30  # 60 daily returns
        legacy = compute_vol_target_scale(rets)
        pinned = compute_vol_target_scale(rets, annualization_days=252.0)
        assert legacy == pinned  # equity byte-identity
        crypto = compute_vol_target_scale(rets, annualization_days=365.0)
        # √365 realized vol is larger → scale strictly smaller.
        assert crypto == pytest.approx(legacy * math.sqrt(252.0 / 365.0))

    def test_qp_sigma_horizon_scale(self):
        from renquant_pipeline.kernel.portfolio_qp.tasks import (
            _qp_sigma_horizon_scale,
        )
        assert _qp_sigma_horizon_scale("annualized", 20) == pytest.approx(
            math.sqrt(20 / 252.0)
        )  # equity pin: default divisor unchanged
        assert _qp_sigma_horizon_scale(
            "annualized", 20, annualization_days=365.0
        ) == pytest.approx(math.sqrt(20 / 365.0))

    def _align_ctx(self, config):
        return SimpleNamespace(
            config=config,
            regime=None,
            _qp_sigma=np.array([0.5]),
        )

    def test_align_horizon_task_uses_asset_class_divisor(self):
        from renquant_pipeline.kernel.portfolio_qp.tasks import (
            AlignQPHorizonUnitsTask,
        )
        base = {
            "rotation": {"joint_actions": {
                "qp_sigma_horizon_mode": "scale",
                "qp_sigma_unit": "annualized",
                "qp_mu_horizon_days": 20,
            }},
        }
        ctx = self._align_ctx(dict(base))
        AlignQPHorizonUnitsTask().run(ctx)
        assert ctx._qp_sigma[0] == pytest.approx(0.5 * math.sqrt(20 / 252.0))
        ctx = self._align_ctx({**base, "asset_class": "crypto"})
        AlignQPHorizonUnitsTask().run(ctx)
        assert ctx._qp_sigma[0] == pytest.approx(0.5 * math.sqrt(20 / 365.0))


# ─── P5: wash-sale bypass (crypto = property, §1091 N/A) ────────────────────

class TestP5WashSaleBypass:
    """The §1091 bypass requires BOTH asset_class="crypto" AND the ticker
    being an explicitly validated non-security spot pair (Codex hardening,
    pipeline#183) — "AAPL" is used for the equity-pin cases (unaffected by
    any of this); "BTC/USD" (validated) and "XYZ-TOKEN" (asset_class=crypto
    but NEVER in the validated set — modeling a tokenized-security-style
    instrument wrongly routed through the crypto asset class) cover the two
    required branches for every consumer."""

    LOSS_SALE = {"AAPL": dt.date(2026, 6, 25), "BTC/USD": dt.date(2026, 6, 25),
                 "XYZ-TOKEN": dt.date(2026, 6, 25)}   # 3 days before SUN
    LOSS_PLS = {"AAPL": -50.0, "BTC/USD": -50.0, "XYZ-TOKEN": -50.0}
    VALIDATED = frozenset({"BTC/USD"})

    def test_gate_blocks_equity_never_crypto(self):
        from renquant_pipeline.kernel.selection import (
            is_wash_sale_blocked,
            is_wash_sale_blocked_with_cost,
        )
        blocked, reason, cost = is_wash_sale_blocked_with_cost(
            "AAPL", SUN, self.LOSS_SALE, self.LOSS_PLS, 30,
        )
        assert blocked  # equity pin: recent loss sale blocks
        blocked, reason, cost = is_wash_sale_blocked_with_cost(
            "BTC/USD", SUN, self.LOSS_SALE, self.LOSS_PLS, 30,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )
        assert not blocked and "1091" in reason and cost == 0.0
        # Unknown-P/L conservative branch also bypassed for VALIDATED crypto.
        assert is_wash_sale_blocked_with_cost(
            "AAPL", SUN, self.LOSS_SALE, None, 30,
        )[0] is True
        assert is_wash_sale_blocked_with_cost(
            "BTC/USD", SUN, self.LOSS_SALE, None, 30,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )[0] is False
        # Legacy binary helper too.
        assert is_wash_sale_blocked("AAPL", SUN, self.LOSS_SALE, 30) is True
        assert is_wash_sale_blocked(
            "BTC/USD", SUN, self.LOSS_SALE, 30,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        ) is False

    def test_asset_class_crypto_alone_is_not_sufficient_fail_closed(self):
        """The finding this round fixes: asset_class="crypto" tagging a
        ticker is NOT itself sufficient for the §1091 bypass — an unknown/
        unvalidated/tokenized-security-shaped ticker must fail closed (rule
        STILL applies), never silently inherit the crypto exemption."""
        from renquant_pipeline.kernel.asset_class import (
            is_validated_crypto_spot_pair,
            wash_sale_applies_for_ticker,
        )
        from renquant_pipeline.kernel.selection import (
            is_wash_sale_blocked,
            is_wash_sale_blocked_with_cost,
        )
        assert not is_validated_crypto_spot_pair("XYZ-TOKEN", self.VALIDATED)
        assert wash_sale_applies_for_ticker("crypto", "XYZ-TOKEN", self.VALIDATED) is True
        # No validated_crypto_pairs supplied at all (the historical call
        # shape) -> nobody is validated -> §1091 still applies, even to a
        # plausible-looking pair.
        assert wash_sale_applies_for_ticker("crypto", "BTC/USD", None) is True
        blocked, reason, cost = is_wash_sale_blocked_with_cost(
            "XYZ-TOKEN", SUN, self.LOSS_SALE, self.LOSS_PLS, 30,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )
        assert blocked and "1091" not in reason
        assert is_wash_sale_blocked(
            "XYZ-TOKEN", SUN, self.LOSS_SALE, 30,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        ) is True

    def test_resolve_validated_crypto_spot_pairs_from_config(self):
        from renquant_pipeline.kernel.asset_class import (
            resolve_validated_crypto_spot_pairs,
        )
        assert resolve_validated_crypto_spot_pairs({}) == frozenset()
        assert resolve_validated_crypto_spot_pairs(None) == frozenset()
        assert resolve_validated_crypto_spot_pairs(
            {"crypto_spot_pairs": ["BTC/USD", "eth-usd"]}
        ) == frozenset({"BTC/USD", "ETH/USD"})
        # Malformed entries drop rather than raise or false-match.
        assert resolve_validated_crypto_spot_pairs(
            {"crypto_spot_pairs": ["BTC/USD", "not-a-pair-form!!"]}
        ) == frozenset({"BTC/USD"})

    def test_candidate_filter_task(self):
        from renquant_pipeline.kernel.pipeline.task_candidates import (
            WashSaleFilterTask,
        )
        def _tc(ticker, cfg):
            return SimpleNamespace(
                ticker=ticker, today=SUN, config=cfg,
                last_sell_dates=dict(self.LOSS_SALE),
                last_sell_pls=dict(self.LOSS_PLS),
                blocked_by=None,
            )
        tc = _tc("AAPL", {"wash_sale_days": 30})
        assert WashSaleFilterTask().run(tc) is False  # equity pin: blocked
        assert str(tc.blocked_by).startswith("wash_sale")
        crypto_cfg = {
            "wash_sale_days": 30, "asset_class": "crypto",
            "crypto_spot_pairs": ["BTC/USD"],
        }
        tc = _tc("BTC/USD", crypto_cfg)
        assert WashSaleFilterTask().run(tc) is None   # validated crypto: passes
        assert tc.blocked_by is None
        # asset_class=crypto but NOT in the validated set -> still blocked.
        tc = _tc("XYZ-TOKEN", crypto_cfg)
        assert WashSaleFilterTask().run(tc) is False
        assert str(tc.blocked_by).startswith("wash_sale")

    def test_qp_mask_wash_leg_bypassed_for_validated_crypto_only(self):
        from renquant_pipeline.kernel.portfolio_qp.tasks import (
            _compute_qp_wash_mask,
        )
        kwargs = dict(
            tickers=["BTC/USD"], today=SUN,
            last_sell_dates=dict(self.LOSS_SALE),
            last_sell_pls=dict(self.LOSS_PLS),
            wash_days=30, min_reentry=0,
            held_tickers=set(), calibrator_saturated=False,
        )
        mask, n_wash, _, _ = _compute_qp_wash_mask(
            tickers=["AAPL"], today=SUN,
            last_sell_dates=dict(self.LOSS_SALE),
            last_sell_pls=dict(self.LOSS_PLS),
            wash_days=30, min_reentry=0,
            held_tickers=set(), calibrator_saturated=False,
        )
        assert mask[0] and n_wash == 1  # equity pin
        mask, n_wash, _, _ = _compute_qp_wash_mask(
            **kwargs, asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )
        assert not mask[0] and n_wash == 0
        # asset_class=crypto but NOT validated -> still blocked.
        mask, n_wash, _, _ = _compute_qp_wash_mask(
            tickers=["XYZ-TOKEN"], today=SUN,
            last_sell_dates=dict(self.LOSS_SALE),
            last_sell_pls=dict(self.LOSS_PLS),
            wash_days=30, min_reentry=0,
            held_tickers=set(), calibrator_saturated=False,
            asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )
        assert mask[0] and n_wash == 1
        # Anti-churn leg is NOT §1091 — still applies to validated crypto.
        kwargs["min_reentry"] = 10
        mask, n_wash, n_churn, _ = _compute_qp_wash_mask(
            **kwargs, asset_class="crypto", validated_crypto_pairs=self.VALIDATED,
        )
        assert mask[0] and n_wash == 0 and n_churn == 1

    def test_selection_loop_bypasses_wash_for_validated_crypto_only(self):
        from renquant_pipeline.kernel.selection import (
            CandidateResult,
            SelectionContext,
            run_selection_loop,
        )
        def _ctx(ticker, asset_class, validated=frozenset()):
            return SelectionContext(
                today=SUN, held_tickers=[],
                last_sell_dates=dict(self.LOSS_SALE),
                last_sell_pls=dict(self.LOSS_PLS),
                earnings_calendar={}, corr_matrix={}, sector_map={ticker: "IT"},
                defensive_set=set(), wash_sale_days=30, earnings_buffer=0,
                corr_threshold=0.99, max_per_sector=0, tiered_thresholds=[],
                open_slots=1, asset_class=asset_class,
                validated_crypto_spot_pairs=validated,
            )
        equity_cand = [CandidateResult("AAPL", 1.0, 1.0, 0.0)]
        selected, blocks = run_selection_loop(equity_cand, _ctx("AAPL", "us_equity"))
        assert selected == [] and blocks["wash_sale"] == 1  # equity pin
        crypto_cand = [CandidateResult("BTC/USD", 1.0, 1.0, 0.0)]
        selected, blocks = run_selection_loop(
            crypto_cand, _ctx("BTC/USD", "crypto", self.VALIDATED)
        )
        assert selected == ["BTC/USD"] and blocks["wash_sale"] == 0
        # asset_class=crypto but the ticker is NOT in the validated set.
        unvalidated_cand = [CandidateResult("XYZ-TOKEN", 1.0, 1.0, 0.0)]
        selected, blocks = run_selection_loop(
            unvalidated_cand, _ctx("XYZ-TOKEN", "crypto", self.VALIDATED)
        )
        assert selected == [] and blocks["wash_sale"] == 1

    def test_crypto_sell_does_not_stamp_reentry_state(self):
        """THE RFC-required pin: a sell of a VALIDATED crypto spot pair must
        not stamp/block re-entry while an equity one still does
        (StampWashSaleTask → gate). An asset_class=crypto sell of an
        UNVALIDATED ticker must still stamp (Codex hardening, pipeline#183)
        — otherwise the ticker-level fail-closed block has nothing to
        check the re-entry against."""
        from renquant_pipeline.kernel.execution.types import Fill, OrderSide
        from renquant_pipeline.kernel.pipeline.task_execution import (
            StampWashSaleTask,
        )
        from renquant_pipeline.kernel.selection import (
            is_wash_sale_blocked_with_cost,
        )

        def _sell_ctx(ticker, config):
            fill = Fill(
                ticker=ticker, side=OrderSide.SELL, shares=5.0, price=10.0,
                fees=0.0, today=pd.Timestamp(FRI),
            )
            backend = SimpleNamespace(get_position_quantity=lambda t: 0.0)
            return SimpleNamespace(
                fills=[fill],
                exits=[(ticker, SimpleNamespace(exit_type="stop_loss"))],
                last_sell_dates={}, last_stop_exit_dates={},
                today=pd.Timestamp(FRI), config=config,
                execution_backend=backend,
            )

        # Equity pin: full-liquidate sell stamps the wash-sale date and the
        # gate then blocks a re-entry inside the window.
        ctx = _sell_ctx("XYZ", {})
        StampWashSaleTask().run(ctx)
        assert ctx.last_sell_dates == {"XYZ": FRI}
        assert ctx.last_stop_exit_dates == {"XYZ": FRI}
        blocked, _, _ = is_wash_sale_blocked_with_cost(
            "XYZ", SUN, ctx.last_sell_dates, None, 30,
        )
        assert blocked

        # Validated crypto: the SAME sell stamps NOTHING into wash-sale
        # state (§1091 N/A — no re-entry state may exist), while the G8
        # post-stop cooldown stamp (a risk rail, not tax law) still fires.
        crypto_cfg = {"asset_class": "crypto", "crypto_spot_pairs": ["BTC/USD"]}
        ctx = _sell_ctx("BTC/USD", crypto_cfg)
        StampWashSaleTask().run(ctx)
        assert ctx.last_sell_dates == {}
        assert ctx.last_stop_exit_dates == {"BTC/USD": FRI}
        # Belt-and-suspenders: even a (stale/foreign) stamped date cannot
        # block a validated-crypto re-entry — the gate is bypassed.
        blocked, _, _ = is_wash_sale_blocked_with_cost(
            "BTC/USD", SUN, {"BTC/USD": FRI}, None, 30,
            asset_class="crypto", validated_crypto_pairs=frozenset({"BTC/USD"}),
        )
        assert not blocked

        # asset_class=crypto but the ticker is NOT validated: §1091
        # genuinely applies, so the sell date MUST still be stamped, or the
        # (correctly fail-closed) block below would have nothing to block
        # against on a re-entry attempt.
        ctx = _sell_ctx("XYZ-TOKEN", crypto_cfg)
        StampWashSaleTask().run(ctx)
        assert ctx.last_sell_dates == {"XYZ-TOKEN": FRI}
        blocked, _, _ = is_wash_sale_blocked_with_cost(
            "XYZ-TOKEN", SUN, ctx.last_sell_dates, None, 30,
            asset_class="crypto", validated_crypto_pairs=frozenset({"BTC/USD"}),
        )
        assert blocked

    def test_governor_sizing_no_buy_mask_bypassed_for_validated_crypto_only(self):
        """Codex re-review of #183 (7aa82cf5): run_governor_sizing's no-buy
        wash-sale mask called is_wash_sale_blocked with no asset_class/
        validated_crypto_pairs kwargs, so it always evaluated under the
        us_equity default — a validated crypto pair recently sold at a loss
        was wrongly blocked from a top-up. An asset_class="crypto"-tagged
        but unvalidated ticker must still be blocked (fail closed); an
        equity ticker is unaffected."""
        from renquant_pipeline.context import InferenceContext
        from renquant_pipeline.kernel.exits import HoldingState
        from renquant_pipeline.kernel.pipeline.task_selection import SizeAndEmitTask

        gov_cfg = {
            "enabled": True,
            "e_ceil_by_regime": {"BULL_CALM": 0.95},
            "hysteresis_band": 0.0,
            "kelly_fraction": 0.3,
            "mu_shrinkage": 0.0,
            "top_k": 8,
            "max_step_per_session": 1.0,
        }

        def _held():
            return HoldingState(
                entry_price=100.0, entry_date=SUN - dt.timedelta(days=100),
                high_watermark=100.0, shares=5, mu=0.04, sigma=0.2,
            )

        def _cfg(**top):
            cfg = {
                "regime_params": {"BULL_CALM": {
                    "max_position_pct": 0.12, "cash_reserve_pct": 0.0,
                    "max_concurrent_positions": 8,
                }},
                "ranking": {"panel_scoring": {
                    "enabled": True,
                    "sizing": {"enabled": True, "floor": 0.0, "ceiling": 1.0,
                               "min_mult": 0.5},
                    "sigma_sizing": {},
                }, "kelly_sizing": {"enabled": False}},
                "regime": {}, "wash_sale_days": 30, "min_hold_days": 0,
                "deployment_governor": gov_cfg,
            }
            cfg.update(top)
            return cfg

        def _orders(ticker, cfg, last_sell_dates):
            ctx = InferenceContext(
                config=cfg, today=SUN, regime="BULL_CALM", confidence=1.0,
                bear_only=False, portfolio_value=10_000.0, cash=5_000.0,
                prices={ticker: 100.0}, ranked=[], models={},
                holdings={ticker: _held()},
                last_sell_dates=last_sell_dates,
            )
            ctx._selected = []  # noqa: SLF001
            SizeAndEmitTask().run(ctx)
            return {o["ticker"] for o in ctx.orders}

        assert "BTC/USD" in _orders(
            "BTC/USD",
            _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
            {"BTC/USD": SUN - dt.timedelta(days=5)},
        )
        assert "XYZ-TOKEN" not in _orders(
            "XYZ-TOKEN",
            _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
            {"XYZ-TOKEN": SUN - dt.timedelta(days=5)},
        )
        assert "AAPL" not in _orders(
            "AAPL", _cfg(), {"AAPL": SUN - dt.timedelta(days=5)},
        )

    def test_governor_sizing_no_sell_1091_guard_bypassed_for_validated_crypto_only(
        self, monkeypatch,
    ):
        """Found while fixing the no-buy threading gap above: run_governor_
        sizing's INLINE §1091 no-sell guard (blocking the sale of a loss
        lot bought inside the wash-sale window) was not asset-class-aware
        at all — it never called wash_sale_applies_for_ticker, so a
        validated crypto pair held at a loss inside the window was wrongly
        floored at its current weight even though §1091 never applies to
        it. An asset_class="crypto"-tagged but unvalidated ticker, and a
        plain equity ticker, must still be floored (fail closed / pinned)."""
        import renquant_pipeline.kernel.pipeline.governor_sizing as gs_mod
        from renquant_pipeline.context import InferenceContext
        from renquant_pipeline.kernel.exits import HoldingState

        gov_cfg = {
            "enabled": True,
            "e_ceil_by_regime": {"BULL_CALM": 0.95},
            "hysteresis_band": 0.0,
            "kelly_fraction": 0.3,
            "mu_shrinkage": 0.0,
            "top_k": 8,
            "max_step_per_session": 1.0,
        }

        def _held():
            return HoldingState(
                entry_price=100.0, entry_date=SUN - dt.timedelta(days=5),
                high_watermark=100.0, shares=5, mu=0.04, sigma=0.2,
            )

        def _cfg(**top):
            cfg = {
                "regime_params": {"BULL_CALM": {
                    "max_position_pct": 0.12, "cash_reserve_pct": 0.0,
                    "max_concurrent_positions": 8,
                }},
                "ranking": {"panel_scoring": {
                    "enabled": True,
                    "sizing": {"enabled": True, "floor": 0.0, "ceiling": 1.0,
                               "min_mult": 0.5},
                    "sigma_sizing": {},
                }, "kelly_sizing": {"enabled": False}},
                "regime": {}, "wash_sale_days": 30, "min_hold_days": 0,
                "deployment_governor": gov_cfg,
            }
            cfg.update(top)
            return cfg

        def _no_sell_for(ticker, cfg):
            captured: dict[str, set] = {}
            real_allocate = gs_mod.allocate_down_only

            def _spy(**kwargs):
                captured["no_sell"] = set(kwargs["no_sell"])
                return real_allocate(**kwargs)

            monkeypatch.setattr(gs_mod, "allocate_down_only", _spy)
            ctx = InferenceContext(
                config=cfg, today=SUN, regime="BULL_CALM", confidence=1.0,
                bear_only=False, portfolio_value=10_000.0, cash=5_000.0,
                # Held at a loss: entry 100.0, current price 50.0.
                prices={ticker: 50.0}, ranked=[], models={},
                holdings={ticker: _held()}, last_sell_dates={},
            )
            ctx._selected = []  # noqa: SLF001
            gs_mod.run_governor_sizing(ctx, gov_cfg)
            return captured["no_sell"]

        assert "BTC/USD" not in _no_sell_for(
            "BTC/USD", _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
        )
        assert "XYZ-TOKEN" in _no_sell_for(
            "XYZ-TOKEN", _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
        )
        assert "AAPL" in _no_sell_for("AAPL", _cfg())

    def test_joint_action_wash_check_bypassed_for_validated_crypto_only(self):
        """Codex re-review of #183 (7aa82cf5): JointActionTask's buy-leg
        wash-sale check called is_wash_sale_blocked_with_cost with no
        asset_class/validated_crypto_pairs kwargs. Prove the joint_blocked_wash
        counter — and the actual buy — reflect the ticker-scoped policy."""
        from renquant_pipeline.context import InferenceContext
        from renquant_pipeline.kernel.selection import CandidateResult
        from renquant_pipeline.kernel.pipeline.task_joint_actions import JointActionTask

        def _cand(ticker):
            return CandidateResult(
                ticker=ticker, raw_score=0.5, rank_score=0.6, rs_score=0.0,
                detail="", expected_return=0.04, expected_return_horizon_days=60,
                panel_score=0.5, mu=0.04, mu_horizon_days=60, sigma=0.2,
            )

        def _cfg(**top):
            cfg = {
                "regime_params": {"BULL_CALM": {
                    "max_position_pct": 0.10, "cash_reserve_pct": 0.0,
                    "max_concurrent_positions": 8,
                }},
                "ranking": {"panel_scoring": {"enabled": True, "sizing": {},
                                              "sigma_sizing": {}},
                            "kelly_sizing": {"enabled": False}},
                "regime": {},
                "rotation": {"joint_actions": {"enabled": True, "solver": "greedy"}},
                "max_positions_per_sector": 0,
                "wash_sale_days": 30,
            }
            cfg.update(top)
            return cfg

        def _run(ticker, cfg, last_sell_dates):
            ctx = InferenceContext(
                config=cfg, today=SUN, regime="BULL_CALM", confidence=1.0,
                bear_only=False, portfolio_value=10_000.0, cash=10_000.0,
                prices={ticker: 100.0}, ranked=[_cand(ticker)], models={},
                holdings={}, last_sell_dates=last_sell_dates,
                last_sell_pls={t: -50.0 for t in last_sell_dates},
            )
            ctx._selected = []  # noqa: SLF001
            JointActionTask().run(ctx)
            return ctx

        ctx = _run(
            "BTC/USD",
            _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
            {"BTC/USD": SUN - dt.timedelta(days=5)},
        )
        assert ctx.counters.get("joint_blocked_wash", 0) == 0
        assert any(o["ticker"] == "BTC/USD" for o in ctx.orders)

        ctx = _run(
            "XYZ-TOKEN",
            _cfg(asset_class="crypto", crypto_spot_pairs=["BTC/USD"]),
            {"XYZ-TOKEN": SUN - dt.timedelta(days=5)},
        )
        assert ctx.counters.get("joint_blocked_wash", 0) == 1
        assert not any(o["ticker"] == "XYZ-TOKEN" for o in ctx.orders)

        ctx = _run("AAPL", _cfg(), {"AAPL": SUN - dt.timedelta(days=5)})
        assert ctx.counters.get("joint_blocked_wash", 0) == 1  # equity pin

    def test_validate_pairs_task_wash_mask_bypassed_for_validated_crypto_only(self):
        """Codex re-review of #183 (7aa82cf5): ValidatePairsTask's rotation
        wash-sale check called is_wash_sale_blocked_with_cost with no
        asset_class/validated_crypto_pairs kwargs. A validated crypto pair
        must survive the rotation guard; an asset_class="crypto"-tagged but
        unvalidated buy ticker must still be dropped."""
        from renquant_pipeline.kernel.rotation import RotationPair
        from renquant_pipeline.kernel.pipeline.task_rotation import ValidatePairsTask

        def _pair(sell_ticker, buy_ticker):
            return RotationPair(
                sell_ticker=sell_ticker, buy_ticker=buy_ticker,
                sell_score=0.3, buy_score=0.6, sell_er=0.0, buy_er=0.05,
                horizon_days=20, raw_advantage=0.05, tax_drag=0.0,
                transaction_cost=0.001, net_advantage=0.049,
                threshold=0.02, margin_realized=0.029,
            )

        def _run(sell_ticker, buy_ticker, cfg, last_sell_dates):
            ctx = SimpleNamespace(
                config=cfg, today=SUN, holdings={}, corr_matrix={},
                last_sell_dates=last_sell_dates,
                last_sell_pls={t: -50.0 for t in last_sell_dates},
                rotations=[_pair(sell_ticker, buy_ticker)],
            )
            ValidatePairsTask().run(ctx)
            return ctx.rotations

        cfg = {"wash_sale_days": 30, "asset_class": "crypto",
               "crypto_spot_pairs": ["BTC/USD"]}
        survived = _run("OLD", "BTC/USD", cfg,
                         {"BTC/USD": SUN - dt.timedelta(days=5)})
        assert [p.buy_ticker for p in survived] == ["BTC/USD"]

        survived = _run("OLD", "XYZ-TOKEN", cfg,
                         {"XYZ-TOKEN": SUN - dt.timedelta(days=5)})
        assert survived == []

        equity_cfg = {"wash_sale_days": 30}
        survived = _run("OLD", "AAPL", equity_cfg,
                         {"AAPL": SUN - dt.timedelta(days=5)})
        assert survived == []  # equity pin


# ─── P6: tax property-mode (verify-only — no code change) ───────────────────

class TestP6TaxPropertyMode:
    def test_crypto_sell_decision_consults_no_wash_state(self):
        """P6 pin (RFC §3.4): ST/LT holding-period tax treatment applies to
        crypto AS-IS; the only tax machinery keyed off asset class is §1091.
        A crypto config simply omits wash-sale knobs — and even when present
        they are inert (bypass proven in TestP5WashSaleBypass)."""
        from renquant_pipeline.kernel.selection import (
            is_wash_sale_blocked_with_cost,
        )
        blocked, reason, cost = is_wash_sale_blocked_with_cost(
            "BTC/USD", SUN, {"BTC/USD": SAT}, {"BTC/USD": -500.0},
            30, asset_class="crypto",
            validated_crypto_pairs=frozenset({"BTC/USD"}),
        )
        assert (blocked, cost) == (False, 0.0)
        # The LT threshold machinery is untouched: 365-day property
        # holding-period classification stays available to crypto.
        from renquant_pipeline.kernel.pipeline.soft_exit_guards import (
            lt_gate_suppression,
        )
        suppressed, _ = lt_gate_suppression(
            config={"lt_hold_gate_days": 30, "lt_hold_min_gain": 0.10},
            today=SUN,
            holding=SimpleNamespace(
                entry_date=SUN - dt.timedelta(days=60), entry_price=100.0,
            ),
            current_price=150.0,
        )
        assert suppressed  # same ST/LT window logic, asset-class-agnostic


# ─── P7: σ-clip bounds per asset class ──────────────────────────────────────

def _ohlcv_from_returns(daily_ret: float, n: int = 80) -> pd.DataFrame:
    """Deterministic alternating ±daily_ret close series."""
    closes, price = [], 100.0
    for i in range(n):
        price *= (1.0 + daily_ret) if i % 2 == 0 else 1.0 / (1.0 + daily_ret)
        closes.append(price)
    idx = pd.date_range("2026-03-01", periods=n, freq="D")
    return pd.DataFrame({"close": closes}, index=idx)


class TestP7VolClipBounds:
    def _run_fallback(self, config, df):
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            ApplyRealizedVolFallbackTask,
        )
        from renquant_pipeline.kernel.selection import CandidateResult

        cand = CandidateResult("XYZ", 1.0, 1.0, 0.0)
        ctx = SimpleNamespace(
            config=config, ohlcv={"XYZ": df}, candidates=[cand], holdings={},
        )
        ApplyRealizedVolFallbackTask().run(ctx)
        return cand.sigma

    KELLY_ON = {"ranking": {"kelly_sizing": {"use_realized_vol_fallback": True}}}

    def test_ceiling_default_equity_pins_150_crypto_300(self):
        wild = _ohlcv_from_returns(0.12)  # σ_ann(252) ≈ 1.9, σ_ann(365) ≈ 2.3
        sigma = self._run_fallback(dict(self.KELLY_ON), wild)
        assert sigma == pytest.approx(1.50)  # equity pin: legacy ceiling
        sigma = self._run_fallback({**self.KELLY_ON, "asset_class": "crypto"}, wild)
        assert 1.50 < sigma < 3.00  # crypto ceiling no longer pins vol

    def test_floor_default_equity_005_crypto_020(self):
        quiet = _ohlcv_from_returns(0.0001)
        sigma = self._run_fallback(dict(self.KELLY_ON), quiet)
        assert sigma == pytest.approx(0.05)
        sigma = self._run_fallback({**self.KELLY_ON, "asset_class": "crypto"}, quiet)
        assert sigma == pytest.approx(0.20)

    def test_explicit_config_overrides_win_for_both(self):
        wild = _ohlcv_from_returns(0.12)
        override = {"ranking": {"kelly_sizing": {
            "use_realized_vol_fallback": True, "realized_vol_ceiling": 0.80,
        }}}
        assert self._run_fallback(dict(override), wild) == pytest.approx(0.80)
        assert self._run_fallback(
            {**override, "asset_class": "crypto"}, wild
        ) == pytest.approx(0.80)

    def test_risk_gate_annualizes_365_for_crypto(self):
        from renquant_pipeline.kernel.pipeline.task_risk_gates import (
            RealizedVolGateTask,
        )
        df = _ohlcv_from_returns(0.02)  # σ_ann(252) ≈ 0.32, σ_ann(365) ≈ 0.38

        def _run(config):
            cand = SimpleNamespace(ticker="XYZ")
            ctx = SimpleNamespace(
                config=config, candidates=[cand], ohlcv={"XYZ": df}, counters={},
            )
            RealizedVolGateTask().run(ctx)
            return [c.ticker for c in ctx.candidates]

        cfg = {"risk_gates": {"realized_vol": {"max_annualized": 0.35}}}
        assert _run(dict(cfg)) == ["XYZ"]  # equity pin: 0.32 < 0.35 kept
        # crypto: SAME series annualizes √365 → 0.38 > 0.35 dropped.
        assert _run({**cfg, "asset_class": "crypto"}) == []
