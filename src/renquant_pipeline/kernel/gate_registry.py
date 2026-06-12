"""GateRegistry — one decision choke point with a formal verdict algebra.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III /
S2-PR4 + errata C (formal spec, required before extraction); prototype
with 2000-trial property proofs:
scripts/engineering/gate_registry_prototype.py (orchestrator PR #112).

Algebra (errata C, verbatim):
  * Verdict lattice: ``allow(0) < halve(1) < block(2)`` — totally ordered.
  * Aggregate per scope = **max** (join) over submitted verdicts ⇒ gates
    are **risk-monotone**: adding a gate can never increase permissiveness.
  * ``halve`` composes multiplicatively with sizing (``0.5**k`` for k
    halvers), applied BEFORE caps; ``block`` zeroes; risk-class exits are
    OUTSIDE the lattice (they act on positions, not admissions).
  * Determinism: aggregation is order-independent (max and product are
    commutative).

Scopes: ``"book"`` verdicts apply to every ticker; a ticker scope applies
to that ticker only. ``aggregate(ticker)`` therefore joins book + ticker
submissions.

Migration contract (S2-PR4): the 17 direct ``ctx.buy_blocked = True``
writers (census: scripts/engineering/census_ci.py, authoritative count)
become ``registry.submit(...)`` calls, one writer per PR, each gated by
the DRPH replay corpus (umbrella PR #313). Until a writer is migrated it
keeps writing ``buy_blocked`` directly; the registry is additive alongside.
Every submission is a future decision-ledger row (``ledger_rows``) so
funnel forensics become a SQL query instead of log archaeology.
"""
from __future__ import annotations

from typing import Literal, NamedTuple

Verdict = Literal["allow", "halve", "block"]

_ORDER: dict[str, int] = {"allow": 0, "halve": 1, "block": 2}

BOOK_SCOPE = "book"


class GateVerdict(NamedTuple):
    gate: str            # stable gate id, e.g. "transition_window"
    scope: str           # BOOK_SCOPE or a ticker symbol
    verdict: Verdict
    reason: str          # human-readable, lands in the decision ledger
    inputs: dict         # the values the gate decided on (forensics)


class AggregateDecision(NamedTuple):
    verdict: Verdict
    size_multiplier: float            # 0.5**k for k halvers; 0.0 if blocked
    contributing: tuple[GateVerdict, ...]  # most-restrictive first


class GateRegistry:
    """Collects gate verdicts for one run; computes the aggregate."""

    def __init__(self) -> None:
        self._verdicts: list[GateVerdict] = []

    def submit(self, *, gate: str, scope: str, verdict: Verdict,
               reason: str, inputs: dict | None = None) -> GateVerdict:
        if verdict not in _ORDER:
            raise ValueError(
                f"unknown verdict {verdict!r} (lattice: allow < halve < block); "
                f"risk-class exits do not go through the registry")
        v = GateVerdict(str(gate), str(scope), verdict, str(reason),
                        dict(inputs or {}))
        self._verdicts.append(v)
        return v

    def _in_scope(self, scope: str) -> list[GateVerdict]:
        return [v for v in self._verdicts if v.scope in (BOOK_SCOPE, scope)]

    def aggregate(self, scope: str) -> AggregateDecision:
        """Join (max) over book + scope verdicts; deterministic and
        order-independent by construction (max + product commute)."""
        vs = self._in_scope(scope)
        if not vs:
            return AggregateDecision("allow", 1.0, ())
        top = max(_ORDER[v.verdict] for v in vs)
        verdict: Verdict = next(k for k, o in _ORDER.items() if o == top)  # type: ignore[assignment]
        if verdict == "block":
            mult = 0.0
        else:
            k = sum(1 for v in vs if v.verdict == "halve")
            mult = 0.5 ** k
        ranked = tuple(sorted(vs, key=lambda v: (-_ORDER[v.verdict], v.gate)))
        return AggregateDecision(verdict, mult, ranked)

    def blocked(self, scope: str) -> bool:
        return self.aggregate(scope).verdict == "block"

    def ledger_rows(self, *, run_id: str) -> list[dict]:
        """One append-only row per submission — the decision-ledger feed
        (eng plan §IV: funnel forensics as SQL, not log archaeology)."""
        return [
            {
                "run_id": run_id,
                "gate": v.gate,
                "scope": v.scope,
                "verdict": v.verdict,
                "reason": v.reason,
                "inputs": dict(v.inputs),
            }
            for v in self._verdicts
        ]
