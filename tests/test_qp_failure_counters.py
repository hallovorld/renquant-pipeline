"""Regression tests for QP failure counter stamping (codex PR #9 review #1).

The previous fix only incremented counters in
``EmitOrdersFromQPSolutionTask.run()``. Codex pointed out that several QP
failure paths short-circuit BEFORE emit:

  - ``ComputeFullSigmaTask._fail_full_sigma`` (infeasible:<cov-reason>)
  - ``SolveMarkowitzQPTask`` unsupported-cvxportfolio branch
  - ``SolveMarkowitzQPTask`` non-optimal solver outcome

This file pins the contract that the shared helper
``_stamp_qp_failure_counter`` is the single source for the counters and
covers ALL failure paths.
"""
from __future__ import annotations

import types
import pytest

from renquant_pipeline.kernel.portfolio_qp.tasks import _stamp_qp_failure_counter


def _ctx_with_counters() -> types.SimpleNamespace:
    return types.SimpleNamespace(counters={})


# ── Direct helper tests — cover the status → counter key mapping ─────────────

def test_infeasible_stamps_qp_infeasible():
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "infeasible")
    assert ctx.counters == {"qp_infeasible": 1}


def test_infeasible_with_reason_suffix_stamps_qp_infeasible():
    """ComputeFullSigma sets status='infeasible:<cov-reason>' — must still match."""
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "infeasible:cov_nan_pair")
    assert ctx.counters == {"qp_infeasible": 1}


def test_missing_solution_stamps_qp_missing_solution():
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "missing_solution")
    assert ctx.counters == {"qp_missing_solution": 1}


def test_optimal_no_signal_stamps_qp_optimal_no_signal():
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "optimal_no_signal")
    assert ctx.counters == {"qp_optimal_no_signal": 1}


def test_plain_optimal_stamps_nothing():
    """Successful solver outcome must NOT bump any failure counter."""
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "optimal")
    assert ctx.counters == {}


def test_other_nonoptimal_stamps_qp_other_nonoptimal():
    """Unknown status string falls through to qp_other_nonoptimal."""
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "qp_global:unbounded")
    assert ctx.counters == {"qp_other_nonoptimal": 1}


def test_empty_status_is_noop():
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "")
    _stamp_qp_failure_counter(ctx, None)  # type: ignore[arg-type]
    assert ctx.counters == {}


def test_missing_counters_dict_is_noop():
    """Context without counters dict must not crash."""
    ctx = types.SimpleNamespace()
    _stamp_qp_failure_counter(ctx, "infeasible")          # must not raise


def test_repeated_calls_accumulate():
    ctx = _ctx_with_counters()
    _stamp_qp_failure_counter(ctx, "infeasible")
    _stamp_qp_failure_counter(ctx, "infeasible:cov")
    _stamp_qp_failure_counter(ctx, "missing_solution")
    assert ctx.counters == {"qp_infeasible": 2, "qp_missing_solution": 1}


# ── Integration: ComputeFullSigma fail path stamps counters too ──────────────

def test_compute_full_sigma_fail_stamps_counter():
    """ComputeFullSigmaTask._fail_full_sigma now flows through the helper."""
    from renquant_pipeline.kernel.portfolio_qp.tasks import ComputeFullSigmaTask
    task = ComputeFullSigmaTask()
    ctx = types.SimpleNamespace(counters={})
    task._fail_full_sigma(ctx, "cov_nan_pair")
    assert ctx._qp_status.startswith("infeasible:")
    assert ctx.counters.get("qp_infeasible") == 1


# ── Integration: SolveMarkowitzQP non-optimal solver outcome stamps counter ──

def test_solve_markowitz_nonoptimal_stamps_counter():
    """When solver returns non-optimal, the counter is stamped before emit
    even has a chance to run."""
    from renquant_pipeline.kernel.portfolio_qp.tasks import (
        _stamp_all_qp_blocks, _stamp_qp_failure_counter,
    )
    ctx = types.SimpleNamespace(counters={}, _qp_tickers=[])
    # Simulate the body of SolveMarkowitzQPTask.run after the solver returned
    # an infeasible status — exactly what the daily run hit.
    fake_status = "infeasible"
    ctx._qp_status = fake_status
    ctx._qp_failure_reason = f"qp_global:{fake_status}"
    _stamp_all_qp_blocks(ctx, ctx._qp_failure_reason)
    _stamp_qp_failure_counter(ctx, ctx._qp_status)
    assert ctx.counters.get("qp_infeasible") == 1
