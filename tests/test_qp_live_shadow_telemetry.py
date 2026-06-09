from __future__ import annotations

from dataclasses import dataclass
import json
from types import SimpleNamespace

import numpy as np

from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import AllocatorResult
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import ConstraintSnapshot
from renquant_pipeline.kernel.portfolio_qp.live_shadow_telemetry import (
    EmitQPLiveShadowTelemetryTask,
    append_live_shadow_telemetry_jsonl,
    build_live_shadow_telemetry_envelope,
    load_live_shadow_telemetry_jsonl,
    summarize_live_shadow_telemetry,
    write_live_shadow_summary_json,
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


def _enabled_qp_shadow_ctx(
    tmp_path,
    *,
    config: dict | None = None,
    include_strategy_dir: bool = True,
) -> SimpleNamespace:
    incumbent = _Solution(
        delta_w=np.array([0.02, -0.01, 0.00]),
        target_w=np.array([0.12, 0.04, 0.00]),
    )
    if config is None:
        config = {
            "rotation": {
                "joint_actions": {
                    "qp_live_shadow_telemetry": {
                        "enabled": True,
                        "candidate_name": "hybrid_option_f_allocator",
                        "path": "shadow/qp-live-shadow.jsonl",
                    }
                }
            }
        }
    attrs = {
        "config": config,
        "today": "2026-06-08",
        "broker_name": "alpaca-paper",
        "regime": "BULL_CALM",
        "confidence": 0.72,
        "counters": {},
        "orders": [
            {
                "ticker": "AAPL",
                "order_type": "QP_BUY",
                "source_job": "JointPortfolioQPJob",
                "shares": 2,
            }
        ],
        "exits": [],
        "_qp_constraint_snapshot": _snap(),
        "_qp_solution": incumbent,
        "_qp_mu": np.array([0.03, 0.02, 0.05]),
        "_qp_sigma": np.array([0.30, 0.31, 0.32]),
        "_qp_Sigma_full": None,
        "_active_panel_scorer": {"artifact_id": "hf_patchtst_seed44"},
    }
    if include_strategy_dir:
        attrs["strategy_dir"] = tmp_path
    return SimpleNamespace(**attrs)


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


def test_live_shadow_summary_rolls_up_jsonl_rows(tmp_path) -> None:
    snap = _snap()
    incumbent = _Solution(
        delta_w=np.array([0.02, -0.01, 0.00]),
        target_w=np.array([0.12, 0.04, 0.00]),
    )
    candidate = _Solution(
        delta_w=np.array([0.00, -0.01, 0.08]),
        target_w=np.array([0.10, 0.04, 0.08]),
    )
    rows = []
    for day in ("2026-06-08", "2026-06-09"):
        rows.append(build_live_shadow_telemetry_envelope(
            snap=snap,
            mu=np.array([0.03, 0.02, 0.05]),
            sigma=np.array([0.10, 0.11, 0.12]),
            incumbent_solution=incumbent,
            candidate_solution=candidate,
            candidate_name="hybrid_option_f_allocator",
            as_of_date=day,
        ))
    jsonl = tmp_path / "qp-live-shadow.jsonl"
    for row in rows:
        append_live_shadow_telemetry_jsonl(jsonl, row)

    loaded = load_live_shadow_telemetry_jsonl(jsonl)
    summary = summarize_live_shadow_telemetry(loaded, shadow_days_needed=2)
    out = write_live_shadow_summary_json(
        input_jsonl=jsonl,
        output_json=tmp_path / "qp-live-shadow-summary.json",
        shadow_days_needed=2,
    )

    assert loaded == rows
    assert summary["schema_version"] == "qp-live-shadow-summary-v1"
    assert summary["n_rows"] == 2
    assert summary["shadow_days_logged"] == 2
    assert summary["candidate"] == "hybrid_option_f_allocator"
    assert summary["metrics"]["candidate_infeasible_pct"] == 0.0
    assert summary["promotion_gate"]["ready_for_review"] is True
    assert json.loads(out.read_text(encoding="utf-8")) == summary


def test_live_shadow_summary_blocks_ready_when_snapshot_invalid() -> None:
    summary = summarize_live_shadow_telemetry(
        [{
            "schema_version": "qp-live-shadow-v1",
            "as_of_date": "2026-06-08",
            "incumbent_name": "current_qp",
            "candidate_name": "hybrid_option_f_allocator",
            "incumbent": {"status": "optimal"},
            "candidate": {"status": "infeasible:hard_cap"},
            "divergence": {
                "abs_target_w_l1": 0.3,
                "ticker_overlap_pct": 0.5,
                "delta_w_sign_agreement_pct": 0.0,
            },
            "anomalies": ["qp_constraint_snapshot_invalid"],
        }],
        shadow_days_needed=1,
    )

    assert summary["metrics"]["candidate_infeasible_pct"] == 1.0
    assert summary["anomaly_count_by_type"] == {"qp_constraint_snapshot_invalid": 1}
    assert summary["promotion_gate"] == {
        "ready_for_review": False,
        "snapshot_invalid_count": 1,
    }


def test_emit_qp_live_shadow_telemetry_task_writes_jsonl(tmp_path) -> None:
    ctx = _enabled_qp_shadow_ctx(tmp_path)

    before_orders = list(ctx.orders)
    rc = EmitQPLiveShadowTelemetryTask().run(ctx)

    assert rc is None
    assert ctx.orders == before_orders
    assert ctx.counters["qp_live_shadow_telemetry_rows"] == 1
    out = tmp_path / "shadow" / "qp-live-shadow.jsonl"
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["incumbent_name"] == "current_qp"
    assert row["candidate_name"] == "hybrid_option_f_allocator"
    assert row["panel_artifact"] == "hf_patchtst_seed44"
    assert row["incumbent"]["live_orders_emitted"][0]["ticker"] == "AAPL"
    assert row["candidate"]["would_have_orders"]
    assert ctx._qp_live_shadow_telemetry_status == "written"


def test_emit_qp_live_shadow_telemetry_task_uses_config_strategy_dir(tmp_path) -> None:
    ctx = _enabled_qp_shadow_ctx(
        tmp_path,
        config={
            "_strategy_dir": str(tmp_path),
            "rotation": {
                "joint_actions": {
                    "qp_live_shadow_telemetry": {
                        "enabled": True,
                        "candidate_name": "hybrid_option_f_allocator",
                        "path": "shadow/qp-live-shadow.jsonl",
                    }
                }
            },
        },
        include_strategy_dir=False,
    )

    before_orders = list(ctx.orders)
    rc = EmitQPLiveShadowTelemetryTask().run(ctx)

    assert rc is None
    assert ctx.orders == before_orders
    out = tmp_path / "shadow" / "qp-live-shadow.jsonl"
    assert ctx._qp_live_shadow_telemetry_path == str(out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["candidate_name"] == "hybrid_option_f_allocator"
    assert rows[0]["incumbent"]["live_orders_emitted"][0]["ticker"] == "AAPL"


def test_emit_qp_live_shadow_telemetry_task_default_disabled(tmp_path) -> None:
    ctx = SimpleNamespace(
        config={},
        strategy_dir=tmp_path,
        counters={},
    )

    rc = EmitQPLiveShadowTelemetryTask().run(ctx)

    assert rc is None
    assert not (tmp_path / "artifacts").exists()
    assert ctx.counters == {}
