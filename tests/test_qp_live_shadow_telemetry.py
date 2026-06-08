from __future__ import annotations

from dataclasses import dataclass
import json

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.live_shadow_telemetry import (
    append_live_shadow_telemetry_jsonl,
    build_live_shadow_telemetry_envelope,
)


@dataclass
class _Solution:
    delta_w: np.ndarray
    target_w: np.ndarray
    status: str = "optimal"


def _snap() -> ConstraintSnapshot:
    return ConstraintSnapshot(
        n=3,
        tickers=("AAPL", "MSFT", "TSLA"),
        w_current=np.array([0.10, 0.05, 0.00]),
        w_upper_hard=np.array([0.20, 0.20, 0.15]),
        w_upper=np.array([0.20, 0.20, 0.15]),
        w_lower=0.0,
        dw_max=np.full(3, 0.50),
        cash_reserve=0.0,
        turnover_max=0.50,
        drawdown=0.0,
        drawdown_limit=0.20,
        gross_max=None,
        wash_sale_mask=np.zeros(3, dtype=bool),
        regime="BULL_CALM",
        confidence=0.72,
    )


def test_live_shadow_telemetry_envelope_is_json_safe() -> None:
    snap = _snap()
    incumbent = _Solution(
        delta_w=np.array([0.02, -0.01, 0.00]),
        target_w=np.array([0.12, 0.04, 0.00]),
    )
    candidate = AllocatorResult(
        delta_w=np.array([0.00, -0.01, 0.08]),
        target_w=np.array([0.10, 0.04, 0.08]),
        status="optimal",
        selected_indices=(2,),
    )

    envelope = build_live_shadow_telemetry_envelope(
        snap=snap,
        mu=np.array([0.03, 0.02, 0.05]),
        sigma=np.array([0.10, 0.11, 0.12]),
        incumbent_solution=incumbent,
        candidate_solution=candidate,
        candidate_name="hybrid_option_f",
        as_of_date="2026-06-08",
        as_of_time="2026-06-08T13:00:00-07:00",
        live_orders_emitted=[{"ticker": "AAPL", "side": "buy", "qty": 2}],
        would_have_orders=[{"ticker": "TSLA", "side": "buy", "qty": 8}],
        panel_artifact="hf_patchtst_seed44",
    )

    json.dumps(envelope)
    assert envelope["schema_version"] == "qp-live-shadow-v1"
    assert envelope["constraint_snapshot_contract_version"] == "v1-2026-06-03"
    assert len(envelope["ctx_fingerprint"]) == 64
    assert envelope["regime"] == "BULL_CALM"
    assert envelope["regime_confidence"] == 0.72
    assert envelope["incumbent"]["n_buys"] == 1
    assert envelope["incumbent"]["n_sells"] == 1
    assert envelope["candidate"]["would_have_orders"][0]["ticker"] == "TSLA"
    assert envelope["divergence"]["would_be_missed_alpha"] == ["TSLA"]


def test_live_shadow_telemetry_reports_constraint_violations_and_anomalies() -> None:
    snap = _snap()
    incumbent = _Solution(
        delta_w=np.array([0.00, 0.00, 0.00]),
        target_w=np.array([0.10, 0.05, 0.00]),
        status="cap_compliance_fallback",
    )
    candidate = _Solution(
        delta_w=np.array([0.00, 0.00, 0.30]),
        target_w=np.array([0.10, 0.05, 0.30]),
        status="infeasible:w_upper_hard",
    )

    envelope = build_live_shadow_telemetry_envelope(
        snap=snap,
        mu=np.array([0.03, 0.02, 0.05]),
        sigma=np.array([0.10, 0.11, 0.12]),
        incumbent_solution=incumbent,
        candidate_solution=candidate,
        candidate_name="bad_candidate",
        as_of_date="2026-06-08",
        anomalies=["unit_test_extra"],
    )

    assert envelope["candidate"]["violations_per_family"]["w_upper_hard"] == 1
    assert envelope["anomalies"] == [
        "unit_test_extra",
        "incumbent_qp_used_cap_compliance_fallback",
        "candidate_status=infeasible:w_upper_hard",
    ]


def test_append_live_shadow_telemetry_jsonl(tmp_path) -> None:
    snap = _snap()
    sol = _Solution(
        delta_w=np.array([0.00, 0.00, 0.00]),
        target_w=np.array([0.10, 0.05, 0.00]),
    )
    envelope = build_live_shadow_telemetry_envelope(
        snap=snap,
        mu=np.array([0.03, 0.02, 0.05]),
        sigma=np.array([0.10, 0.11, 0.12]),
        incumbent_solution=sol,
        candidate_solution=sol,
        candidate_name="equal_weight_top_k",
        as_of_date="2026-06-08",
    )

    out = append_live_shadow_telemetry_jsonl(tmp_path / "qp-live-shadow.jsonl", envelope)

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [envelope]
