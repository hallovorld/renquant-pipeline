"""BuyGatesJob — pre-buy gate checks in strict priority order."""
from __future__ import annotations

from .pipeline import Job, Task
from .task_gates import (
    FlattenCooldownGateTask,
    DrawdownGateTask, TransitionWindowTask, ConfidenceVetoTask,
    BullVolOffensiveBlockTask, RegimeAlphaGateTask, BEARBranchTask,
    VelocityCrashTask, EMA50GateTask,
)


class BuyGatesJob(Job):
    """Task chain: FlattenCooldown → DrawdownGate → TransitionWindow →
                  ConfidenceVeto → BullVolOffensiveBlock → RegimeAlphaGate →
                  BEARBranch → VelocityCrash → EMA50

    FlattenCooldownGateTask (2026-05-11) sits FIRST so post-flatten
    cooldown overrides DrawdownGate's resume threshold — see task
    docstring for the S-3 death-spiral motivation. No-op when
    ``risk.drawdown_flatten.cooldown_bars`` is unset or 0.

    BullVolOffensiveBlock sits AFTER ConfidenceVeto (which can already
    force defensives-only on low-confidence regimes) and BEFORE
    BEARBranch (which does the same for BEAR) — BULL_VOL is treated as
    "near-BEAR" when the AA-surfaced IC inversion flag is on.

    RegimeAlphaGateTask (2026-05-20) sits AFTER BullVol and BEFORE BEARBranch:
    per-regime "model has no OOS alpha" block. Sourced from
    artifacts/prod/truly_oos_eval/eval_truly_oos.json — BULL_CALM has
    top-10 OOS alpha −0.045, so we block new buys there.
    """

    @property
    def tasks(self) -> list[Task]:
        return [
            FlattenCooldownGateTask(),
            DrawdownGateTask(),
            TransitionWindowTask(),
            ConfidenceVetoTask(),
            BullVolOffensiveBlockTask(),
            RegimeAlphaGateTask(),
            BEARBranchTask(),
            VelocityCrashTask(),
            EMA50GateTask(),
        ]

    def run(self, ctx) -> None:
        """Run the gate chain, then apply the registry aggregate ONCE.

        The errata-C choke point (eng plan S2-PR4): gate tasks submit
        verdicts instead of writing ``ctx.buy_blocked``; the max-join
        aggregate is applied here, at the job boundary, before any
        downstream job reads the flag. A task returning False still
        short-circuits the chain exactly as before — short-circuit and
        blocking are independent mechanisms.
        """
        super().run(ctx)
        registry = getattr(ctx, "gate_registry", None)
        if registry is not None and registry.blocked("book"):
            ctx.buy_blocked = True
