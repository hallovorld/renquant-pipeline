"""P-SIZING-GATE-KEYS — the divergent-default key cluster must be explicit.

Campaign A3 (2026-07-03 design-compliance audit §5): pipeline runtime
defaults that contradict the strategy-104 value for the same key mean a
lost config key silently flips live semantics with green checks. This
task fails closed on any missing armed key. Presence-only — present-key
behavior is untouched.
"""
from __future__ import annotations

from renquant_pipeline.kernel.preflight import (  # noqa: PLC0415 (legacy bridge)
    PreflightCheck,
    _check_sizing_gate_keys,
)

from ..base import PreflightTask
from ..ctx import PreflightContext


class SizingGateKeysTask(PreflightTask):
    """Fail closed when a divergent-default sizing/gate key is missing."""

    check_name = "P-SIZING-GATE-KEYS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        return _check_sizing_gate_keys(
            ctx.config, ctx.strategy_dir, ctx.run_mode,
        )
