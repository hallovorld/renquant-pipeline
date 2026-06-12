"""GateRegistry algebra tests — errata C property obligations.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md errata C:
(i) permissiveness monotone non-increasing in the gate set,
(ii) aggregate invariant under submission order,
plus block dominance, halve composition 0.5**k, scope semantics, and the
ledger-row feed. Seeded randomized sweeps stand in for hypothesis (not in
the project env), mirroring the prototype's 2000-trial proofs.
"""
from __future__ import annotations

import random

import pytest

from renquant_pipeline.kernel.gate_registry import (
    BOOK_SCOPE,
    GateRegistry,
    GateVerdict,
)

VERDICTS = ("allow", "halve", "block")


def _submit_all(reg: GateRegistry, vs: list[GateVerdict]) -> None:
    for v in vs:
        reg.submit(gate=v.gate, scope=v.scope, verdict=v.verdict,
                   reason=v.reason, inputs=v.inputs)


def _rand_verdicts(rng: random.Random, n: int) -> list[GateVerdict]:
    return [
        GateVerdict(f"g{i}", rng.choice([BOOK_SCOPE, "AAPL", "MU"]),
                    rng.choice(VERDICTS), "r", {})
        for i in range(n)
    ]


class TestAlgebraUnits:

    def test_empty_registry_allows_full_size(self):
        agg = GateRegistry().aggregate("AAPL")
        assert agg == ("allow", 1.0, ())

    def test_block_dominates_and_zeroes(self):
        reg = GateRegistry()
        reg.submit(gate="a", scope="AAPL", verdict="halve", reason="r")
        reg.submit(gate="b", scope="AAPL", verdict="block", reason="r")
        agg = reg.aggregate("AAPL")
        assert agg.verdict == "block"
        assert agg.size_multiplier == 0.0
        assert reg.blocked("AAPL")

    def test_halve_composes_multiplicatively_before_caps(self):
        reg = GateRegistry()
        for g in ("vol", "breadth", "drawdown"):
            reg.submit(gate=g, scope="AAPL", verdict="halve", reason="r")
        agg = reg.aggregate("AAPL")
        assert agg.verdict == "halve"
        assert agg.size_multiplier == pytest.approx(0.5 ** 3)

    def test_book_scope_applies_to_every_ticker(self):
        reg = GateRegistry()
        reg.submit(gate="regime", scope=BOOK_SCOPE, verdict="block",
                   reason="BEAR")
        assert reg.blocked("AAPL") and reg.blocked("MU")

    def test_ticker_scope_does_not_leak(self):
        reg = GateRegistry()
        reg.submit(gate="earnings", scope="AAPL", verdict="block", reason="r")
        assert reg.blocked("AAPL")
        assert not reg.blocked("MU")
        assert reg.aggregate("MU").size_multiplier == 1.0

    def test_contributing_ranked_most_restrictive_first(self):
        reg = GateRegistry()
        reg.submit(gate="z_allow", scope="AAPL", verdict="allow", reason="r")
        reg.submit(gate="a_halve", scope="AAPL", verdict="halve", reason="r")
        agg = reg.aggregate("AAPL")
        assert [v.verdict for v in agg.contributing] == ["halve", "allow"]

    def test_unknown_verdict_rejected(self):
        with pytest.raises(ValueError, match="lattice"):
            GateRegistry().submit(gate="g", scope="AAPL",
                                  verdict="exit", reason="r")  # type: ignore[arg-type]

    def test_ledger_rows_one_per_submission(self):
        reg = GateRegistry()
        reg.submit(gate="a", scope=BOOK_SCOPE, verdict="allow", reason="ok",
                   inputs={"vol": 0.01})
        reg.submit(gate="b", scope="MU", verdict="block", reason="bad")
        rows = reg.ledger_rows(run_id="r1")
        assert len(rows) == 2
        assert rows[0] == {"run_id": "r1", "gate": "a", "scope": "book",
                           "verdict": "allow", "reason": "ok",
                           "inputs": {"vol": 0.01}}


class TestErrataCProperties:
    """Seeded randomized sweeps (2000 trials, mirroring the prototype)."""

    def test_order_independence_and_risk_monotonicity(self):
        rng = random.Random(7)
        for _ in range(2000):
            vs = _rand_verdicts(rng, rng.randint(0, 8))
            r1 = GateRegistry()
            _submit_all(r1, vs)
            a1 = r1.aggregate("AAPL")

            # (ii) order independence
            r2 = GateRegistry()
            _submit_all(r2, rng.sample(vs, len(vs)))
            a2 = r2.aggregate("AAPL")
            assert (a1.verdict, a1.size_multiplier) == (a2.verdict, a2.size_multiplier)

            # (i) risk monotone: one more gate never increases permissiveness
            extra = GateVerdict("extra", rng.choice([BOOK_SCOPE, "AAPL"]),
                                rng.choice(VERDICTS), "r", {})
            _submit_all(r1, [extra])
            a3 = r1.aggregate("AAPL")
            order = {"allow": 0, "halve": 1, "block": 2}
            assert order[a3.verdict] >= order[a1.verdict]
            assert a3.size_multiplier <= a1.size_multiplier + 1e-12

    def test_block_dominance_sweep(self):
        rng = random.Random(44)
        for _ in range(500):
            vs = _rand_verdicts(rng, rng.randint(1, 8))
            reg = GateRegistry()
            _submit_all(reg, vs)
            agg = reg.aggregate("AAPL")
            in_scope = [v for v in vs if v.scope in (BOOK_SCOPE, "AAPL")]
            if any(v.verdict == "block" for v in in_scope):
                assert agg.verdict == "block" and agg.size_multiplier == 0.0
            elif in_scope:
                k = sum(1 for v in in_scope if v.verdict == "halve")
                assert agg.size_multiplier == pytest.approx(0.5 ** k)
            else:
                assert agg == ("allow", 1.0, ())
