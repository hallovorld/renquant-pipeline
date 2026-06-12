"""P-CONFIG-SCHEMA — typed validation of the dangerous config subset.

Wires kernel.config_schema (PR #117) into the preflight battery per the
design's warn-first rollout (renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.2 /
S1-PR3: "schema is additive first (warn-only), fail-closed after one
clean week").

Severity is SOFT by design during the warn window: a violation is logged
and surfaced in the preflight slate but never aborts a run. The strict
flip (soft → hard + mode="strict") is a deliberate one-line follow-up
gated on one clean week of telemetry — do not flip it inside an
unrelated change.
"""
from __future__ import annotations

from renquant_pipeline.kernel.config_schema import validate_strategy_config

from ..base import PreflightTask
from ..ctx import PreflightContext


class ConfigSchemaTask(PreflightTask):
    """P-CONFIG-SCHEMA — config typos die at load, not mid-trade."""

    check_name = "P-CONFIG-SCHEMA"

    def check(self, ctx: PreflightContext):
        from renquant_pipeline.kernel.preflight import PreflightCheck  # noqa: PLC0415

        report = validate_strategy_config(ctx.config, mode="warn")
        if report.ok:
            return PreflightCheck(
                self.check_name, "soft", True,
                f"typed subset valid; {len(report.extra_top_keys)} untyped "
                f"top-level key(s) pass through (gradual-typing telemetry)",
                details={"extra_top_keys": list(report.extra_top_keys)},
            )
        return PreflightCheck(
            self.check_name, "soft", False,
            f"{len(report.errors)} schema violation(s) (warn-only window — "
            f"strict flip pending one clean week): "
            + "; ".join(report.errors[:3]),
            details={"errors": list(report.errors)},
        )
