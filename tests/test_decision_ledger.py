"""Tests for decision_ledger formatter (S5 pipeline-side)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from renquant_pipeline.decision_ledger import (
    format_gate_verdicts,
    format_rotation_decisions,
    format_ticker_decisions,
)


@dataclass
class FakeCandidate:
    ticker: str
    raw_score: float = 0.5
    rank_score: float = 0.6
    rs_score: float = 0.4
    panel_score: float | None = 0.7
    mu: float | None = 0.03
    sigma: float | None = 0.15
    expected_return: float = 0.02


@dataclass
class FakeExitSignal:
    should_exit: bool = True
    reason: str = "trailing_stop"
    exit_type: str = "trailing_stop"


@dataclass
class FakeRotation:
    sell_ticker: str = "AMZN"
    buy_ticker: str = "GOOG"
    net_advantage: float = 0.01
    threshold: float = 0.005
    tax_drag: float = 0.002
    sell_score: float = 0.3
    buy_score: float = 0.8


@dataclass
class FakeAdmission:
    admitted: bool = True
    reason: str = "all regimes admitted"


class FakeCtx:
    def __init__(
        self,
        candidates: list | None = None,
        entries: list | None = None,
        exits: list | None = None,
        rotations: list | None = None,
        blocked_by: dict | None = None,
        holdings: dict | None = None,
        scores: dict | None = None,
        regime: str = "BULL_CALM",
        confidence: float = 0.85,
    ):
        self.candidates = candidates or []
        self.entries = entries or []
        self.exits = exits or []
        self.rotations = rotations or []
        self.blocked_by = blocked_by or {}
        self.holdings = holdings or {}
        self.scores = scores or {}
        self.regime = regime
        self.confidence = confidence
        self._regime_model_admission = None
        self.model_admission = None


BASIC_CONFIG: dict[str, Any] = {
    "strategy_id": "strategy-104",
    "conviction_gate": {"mu_floor": 0.0},
}


class TestFormatGateVerdicts:
    def test_basic_verdicts(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("AAPL"), FakeCandidate("GOOG")],
        )
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(verdicts) == 6
        gates = {v["gate"] for v in verdicts}
        assert gates == {"regime", "model_admission", "conviction", "vol_gate", "wash_sale", "rotation"}
        for v in verdicts:
            assert v["verdict"] in ("allow", "halve", "block")
            assert "scope" in v
            assert "reason" in v
            assert "inputs" in v

    def test_regime_block(self):
        ctx = FakeCtx(regime="BEAR")
        ctx._regime_model_admission = FakeAdmission(admitted=False, reason="BEAR blocked")
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        regime_v = next(v for v in verdicts if v["gate"] == "regime")
        assert regime_v["verdict"] == "block"
        assert "BEAR" in regime_v["reason"]

    def test_no_candidates_conviction_blocks(self):
        ctx = FakeCtx(candidates=[])
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        conv_v = next(v for v in verdicts if v["gate"] == "conviction")
        assert conv_v["verdict"] == "block"
        assert "no candidates" in conv_v["reason"]

    def test_vol_blocked_tickers(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("AAPL")],
            blocked_by={"AAPL": "vol_gate"},
        )
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        vol_v = next(v for v in verdicts if v["gate"] == "vol_gate")
        assert vol_v["verdict"] == "block"

    def test_wash_sale_blocked(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("META")],
            blocked_by={"META": "wash_sale"},
        )
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        ws_v = next(v for v in verdicts if v["gate"] == "wash_sale")
        assert ws_v["verdict"] == "block"

    def test_scope_from_config(self):
        config = {**BASIC_CONFIG, "strategy_id": "strategy-105"}
        ctx = FakeCtx(candidates=[FakeCandidate("AAPL")])
        verdicts = format_gate_verdicts(ctx, config, "run-001", "2026-07-01")
        assert all(v["scope"] == "strategy-105" for v in verdicts)

    def test_rotation_verdict_matches_per_rotation_executed_flag(self):
        # net_advantage > 0 but below threshold on every rotation: the
        # gate-level verdict must agree with format_rotation_decisions()'s
        # per-rotation "executed" bar, not just check the sign.
        subthreshold = [
            FakeRotation(net_advantage=0.003, threshold=0.005),
            FakeRotation(net_advantage=0.001, threshold=0.01),
        ]
        ctx = FakeCtx(candidates=[FakeCandidate("AAPL")], rotations=subthreshold)
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        rotation_v = next(v for v in verdicts if v["gate"] == "rotation")
        assert rotation_v["verdict"] == "halve"
        assert rotation_v["inputs"]["n_viable"] == 0

        decisions = format_rotation_decisions(
            ctx, BASIC_CONFIG, "run-001", "2026-07-01"
        )
        assert not any(d["executed"] for d in decisions)

    def test_rotation_verdict_allows_when_above_threshold(self):
        above = [FakeRotation(net_advantage=0.02, threshold=0.005)]
        ctx = FakeCtx(candidates=[FakeCandidate("AAPL")], rotations=above)
        verdicts = format_gate_verdicts(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        rotation_v = next(v for v in verdicts if v["gate"] == "rotation")
        assert rotation_v["verdict"] == "allow"
        assert rotation_v["inputs"]["n_viable"] == 1


class TestFormatTickerDecisions:
    def test_buy_decision(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("AAPL", mu=0.05)],
            entries=[("AAPL", "buy_signal")],
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["ticker"] == "AAPL"
        assert d["gate"] == "buy"
        assert d["verdict"] == "allow"
        meta = json.loads(d["metadata_json"])
        assert meta["mu"] == 0.05

    def test_blocked_decision(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("META")],
            blocked_by={"META": "wash_sale"},
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["gate"] == "blocked"
        assert d["verdict"] == "block"
        meta = json.loads(d["metadata_json"])
        assert meta["blocked_by"] == "wash_sale"

    def test_sell_decision(self):
        ctx = FakeCtx(
            exits=[("MSFT", FakeExitSignal(reason="trailing_stop"))],
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["ticker"] == "MSFT"
        assert d["gate"] == "sell"
        assert d["verdict"] == "allow"
        meta = json.loads(d["metadata_json"])
        assert meta["exit_reason"] == "trailing_stop"

    def test_hold_decision(self):
        ctx = FakeCtx(
            holdings={"NVDA": {"shares": 10}},
            scores={"NVDA": 0.72},
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        holds = [d for d in decisions if d["gate"] == "hold"]
        assert len(holds) == 1
        assert holds[0]["ticker"] == "NVDA"
        meta = json.loads(holds[0]["metadata_json"])
        assert meta["score"] == 0.72

    def test_no_trade_decision(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("GOOG", mu=0.02)],
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        assert decisions[0]["gate"] == "no_trade"
        assert decisions[0]["verdict"] == "halve"

    def test_mixed_decisions(self):
        ctx = FakeCtx(
            candidates=[
                FakeCandidate("AAPL", mu=0.05),
                FakeCandidate("GOOG", mu=0.02),
                FakeCandidate("META", mu=0.01),
            ],
            entries=[("AAPL", "buy_signal")],
            exits=[("MSFT", FakeExitSignal(reason="stop_loss"))],
            blocked_by={"META": "sector_cap"},
            holdings={"NVDA": {"shares": 5}},
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        by_gate = {d["ticker"]: d["gate"] for d in decisions}
        assert by_gate["AAPL"] == "buy"
        assert by_gate["GOOG"] == "no_trade"
        assert by_gate["META"] == "blocked"
        assert by_gate["MSFT"] == "sell"
        assert by_gate["NVDA"] == "hold"

    def test_exit_already_a_candidate_buy(self):
        ctx = FakeCtx(
            candidates=[FakeCandidate("AAPL")],
            entries=[("AAPL", "buy_signal")],
            exits=[("AAPL", FakeExitSignal(reason="rotation"))],
        )
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        aapl = [d for d in decisions if d["ticker"] == "AAPL"]
        assert len(aapl) == 1
        assert aapl[0]["gate"] == "buy"

    def test_nan_mu_not_above_floor(self):
        c = FakeCandidate("X", mu=float("nan"))
        ctx = FakeCtx(candidates=[c])
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        meta = json.loads(decisions[0]["metadata_json"])
        assert meta["mu"] is None

    def test_empty_ctx(self):
        ctx = FakeCtx()
        decisions = format_ticker_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert decisions == []


class TestFormatRotationDecisions:
    def test_basic_rotation(self):
        r = FakeRotation(sell_ticker="AMZN", buy_ticker="GOOG", net_advantage=0.01, threshold=0.005)
        ctx = FakeCtx(rotations=[r])
        decisions = format_rotation_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["sell_ticker"] == "AMZN"
        assert d["buy_ticker"] == "GOOG"
        assert d["net_advantage"] == 0.01
        assert d["executed"] is True

    def test_below_threshold_not_executed(self):
        r = FakeRotation(sell_ticker="A", buy_ticker="B", net_advantage=0.002, threshold=0.005)
        ctx = FakeCtx(rotations=[r])
        decisions = format_rotation_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert decisions[0]["executed"] is False

    def test_empty_rotations(self):
        ctx = FakeCtx(rotations=[])
        decisions = format_rotation_decisions(ctx, BASIC_CONFIG, "run-001", "2026-07-01")
        assert decisions == []
