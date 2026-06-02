"""P-BROKER-FILL-FRESHNESS: alert when runner-driven activity is stale.

This check surfaces audit finding 9 from the daily decision-tree review: the
strategy can settle into a long no-trade equilibrium without a visible
preflight signal. It intentionally reads runner-emission state from
``monitor_state.last_activity_date`` instead of broker fill history, because
manual fills and broker-side stops are not fresh alpha decisions.
"""
from __future__ import annotations

import datetime as _dt
import json

from renquant_pipeline.kernel.preflight import PreflightCheck

from ..base import PreflightTask
from ..ctx import PreflightContext


class BrokerFillFreshnessTask(PreflightTask):
    """P-BROKER-FILL-FRESHNESS: runner-driven activity within N trading days."""

    check_name = "P-BROKER-FILL-FRESHNESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        if not ctx.broker_name:
            return PreflightCheck(
                self.check_name, "soft", True,
                "no broker_name (dry-run); skip",
            )
        try:
            from renquant_pipeline.kernel.state_paths import resolve_live_state_read
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", True,
                f"state_paths unavailable: {exc}; skip",
            )
        try:
            state_path, _ = resolve_live_state_read(
                ctx.strategy_dir, ctx.broker_name,
            )
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", True,
                f"resolve_live_state_read failed: {exc}; skip",
            )
        if not state_path.exists():
            return PreflightCheck(
                self.check_name, "soft", True,
                f"state file absent at {state_path} (first run?); skip",
            )
        try:
            state = json.loads(state_path.read_text())
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", True,
                f"state file unparseable: {exc}; skip",
            )

        mon = state.get("monitor_state") or {}
        last_runner_str = (
            mon.get("last_activity_date") or mon.get("first_trade_date") or ""
        )
        cfg = (ctx.config.get("monitoring", {}) or {})
        warn_after = int(cfg.get("fill_freshness_warn_after_trading_days", 5))
        hard_after = int(cfg.get("fill_freshness_hard_after_trading_days", 20))

        if not last_runner_str:
            return PreflightCheck(
                self.check_name, "hard", False,
                "no runner-driven activity recorded in monitor_state "
                "(last_activity_date / first_trade_date both absent); "
                f"hard cap {hard_after} trading days. Strategy has never "
                "emitted a runner order on this broker - investigate gates "
                "(regime_admission, gated_buys, QP cap-compliance) before "
                "next live cycle.",
            )
        try:
            last_runner = _dt.date.fromisoformat(last_runner_str.split("T")[0])
        except Exception:
            return PreflightCheck(
                self.check_name, "soft", True,
                f"monitor_state.last_activity_date unparseable "
                f"({last_runner_str!r}); skip",
            )

        today = _dt.date.today()
        try:
            from renquant_pipeline.kernel.exits import _is_nyse_trading_day

            streak_int = 0
            d = last_runner + _dt.timedelta(days=1)
            while d <= today:
                if _is_nyse_trading_day(d):
                    streak_int += 1
                d += _dt.timedelta(days=1)
        except Exception:
            streak_int = max((today - last_runner).days, 0)

        if streak_int >= hard_after:
            return PreflightCheck(
                self.check_name, "hard", False,
                f"no runner-driven activity in {streak_int} trading days "
                f"(hard cap {hard_after}); last_activity={last_runner_str}. "
                "Strategy is dormant - investigate gates (regime_admission, "
                "gated_buys, QP cap-compliance) before next live cycle.",
            )
        if streak_int >= warn_after:
            return PreflightCheck(
                self.check_name, "soft", True,
                f"no runner-driven activity in {streak_int} trading days "
                f"(warn cap {warn_after}); last_activity={last_runner_str}. "
                "Strategy may be stuck in a no-trade equilibrium.",
            )
        return PreflightCheck(
            self.check_name, "hard", True,
            f"last runner-driven activity {last_runner_str} "
            f"({streak_int} trading days ago)",
        )
