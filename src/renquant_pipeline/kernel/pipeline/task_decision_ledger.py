"""S5 decision-ledger write task — persist gate verdicts to the decision DB.

Fail-open: if common modules are not importable (version skew, dependency
missing), logs a WARNING and continues the daily run. S5 is a measurement
substrate, not a trading gate — a missing write degrades analytics but does not
affect trade safety.

Scope: VERDICT-ONLY persistence. ``format_ticker_decisions()`` is called and its
output is counted (``s5_decisions_formatted``) for observability, but per-ticker
decisions are deliberately NOT written to ``decision_outcomes`` from this task.

Writing them here would reintroduce the exact partial-write poisoning bug fixed
in the S5 outcome observer (renquant-orchestrator PR #351): ``decision_outcomes``
rows are meant to be written ATOMICALLY, once per decision, only after all three
forward-return horizons (5d/20d/60d) are available — the observer enforces this.
But ``outcome_observer.pending_decisions()`` treats the mere EXISTENCE of a
``decision_outcomes`` row at ``(as_of, scope, gate)`` grain (no ticker in the
join condition) as "already observed." A verdict-only row inserted here, at
pipeline-run time, would permanently suppress that decision from ever being
picked up by the observer for real forward-return backfill.

A genuine per-ticker decision-ledger substrate needs a separate registry the
observer can read FROM (distinct from decision_outcomes, which the observer
must remain the sole writer of) — that is follow-up design work, not a missing
API call.
"""
from __future__ import annotations

import logging
from typing import Any

from .context import InferenceContext
from .pipeline import Task

log = logging.getLogger("kernel.pipeline.decision_ledger")


class DecisionLedgerWriteTask(Task):
    """Write gate verdicts to the decision ledger. Verdict-only — see module
    docstring for why per-ticker decisions are formatted but not persisted here.

    Reads:
      ctx (full InferenceContext after all gates have run)
      ctx.config["decision_ledger"]["enabled"]  — default False (opt-in)

    Writes:
      ~/renquant-data/decision_ledger.db via renquant_common.decision_ledger (verdicts only)
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
            from renquant_common.decision_ledger import connect, write_verdicts
        except ImportError:
            log.warning(
                "renquant_common.decision_ledger not importable; "
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
                "S5 decision ledger: wrote %d verdicts for %s "
                "(%d per-ticker decisions formatted, not persisted — see module docstring)",
                len(verdicts), date_iso, len(decisions),
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
