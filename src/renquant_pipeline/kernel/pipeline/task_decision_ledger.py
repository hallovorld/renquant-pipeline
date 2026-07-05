"""S5 decision-ledger write task — persist gate verdicts to the orchestrator DB.

Fail-open: if orchestrator modules are not importable (version skew, dependency
missing), logs a WARNING and continues the daily run. S5 is a measurement
substrate, not a trading gate — a missing write degrades analytics but does not
affect trade safety.
"""
from __future__ import annotations

import logging
from typing import Any

from .context import InferenceContext
from .pipeline import Task

log = logging.getLogger("kernel.pipeline.decision_ledger")


class DecisionLedgerWriteTask(Task):
    """Write gate verdicts + per-ticker decisions to the decision ledger.

    Reads:
      ctx (full InferenceContext after all gates have run)
      ctx.config["decision_ledger"]["enabled"]  — default False (opt-in)

    Writes:
      ~/renquant-data/decision_ledger.db via orchestrator modules
    """

    def run(self, ctx: InferenceContext) -> bool | None:
        cfg = (ctx.config.get("decision_ledger") or {})
        if not cfg.get("enabled", False):
            return False

        date_iso = ctx.today.isoformat()
        run_id = (
            getattr(ctx, "run_id", None)
            or getattr(ctx, "_run_id", None)
            or f"{date_iso}-unscoped"
        )

        try:
            from renquant_pipeline.decision_ledger import (
                format_gate_verdicts,
                format_ticker_decisions,
            )
        except ImportError:
            log.warning(
                "decision_ledger formatters not importable; skipping S5 write"
            )
            return False

        verdicts = format_gate_verdicts(ctx, ctx.config, run_id, date_iso)
        decisions = format_ticker_decisions(ctx, ctx.config, run_id, date_iso)

        try:
            from renquant_orchestrator.decision_ledger import connect, write_verdicts
        except ImportError:
            log.warning(
                "renquant_orchestrator.decision_ledger not importable; "
                "S5 verdicts formatted (%d) but not persisted (fail-open)",
                len(verdicts),
            )
            ctx.counters["s5_verdicts_formatted"] = len(verdicts)
            ctx.counters["s5_decisions_formatted"] = len(decisions)
            ctx.counters["s5_write_skipped"] = 1
            return False

        try:
            conn = connect()
            write_verdicts(
                conn,
                run_id=run_id,
                as_of=date_iso,
                verdicts=[
                    {
                        "scope": v["scope"],
                        "gate": v["gate"],
                        "verdict": v["verdict"],
                        "reason": v["reason"],
                        "inputs": v.get("inputs", {}),
                    }
                    for v in verdicts
                ],
            )
            conn.close()
            ctx.counters["s5_verdicts_written"] = len(verdicts)
            ctx.counters["s5_decisions_formatted"] = len(decisions)
            log.info(
                "S5 decision ledger: wrote %d verdicts, %d decisions for %s",
                len(verdicts), len(decisions), date_iso,
            )
        except Exception:
            log.exception(
                "S5 decision ledger write failed (fail-open); "
                "%d verdicts lost for %s",
                len(verdicts), date_iso,
            )
            ctx.counters["s5_write_error"] = 1
            return False

        return True
