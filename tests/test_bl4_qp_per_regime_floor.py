"""BL-4 regression: QP admission knobs must resolve per-regime, not silently
fall through to a permissive global.

BL-4 (decision-tree deep audit, 2026-06-10): ``_qp_admission_gate_value``
returned the flat global when a ``{key}_by_regime`` map lacked the live regime.
Prod set ``min_expected_return_by_regime={BULL_CALM: 0.01}`` with NO global, so
the expected-return floor disabled itself in BULL_VOLATILE / CHOPPY / BEAR.
These tests pin the corrected resolution order: exact regime → ``default`` key
→ explicit flat global fallback (with a deduped warning) → fail closed.
"""
from __future__ import annotations

import logging

from renquant_pipeline.kernel.portfolio_qp import tasks as T


def _reset_warn_dedup() -> None:
    T._QP_REGIME_FALLTHROUGH_WARNED.clear()


def test_exact_regime_entry_wins() -> None:
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01, "BEAR": 0.03}}
    assert T._qp_admission_gate_value(gate, "min_expected_return", "BULL_CALM") == 0.01
    assert T._qp_admission_gate_value(gate, "min_expected_return", "BEAR") == 0.03


def test_default_key_covers_unlisted_regimes() -> None:
    """The fix the operator should adopt: a `default` baseline for regimes
    not explicitly listed."""
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01, "default": 0.005}}
    assert T._qp_admission_gate_value(gate, "min_expected_return", "CHOPPY") == 0.005
    assert T._qp_admission_gate_value(gate, "min_expected_return", "BEAR") == 0.005
    # exact still beats default
    assert T._qp_admission_gate_value(gate, "min_expected_return", "BULL_CALM") == 0.01


def test_underscore_default_key_also_supported() -> None:
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01, "_default": 0.004}}
    assert T._qp_admission_gate_value(gate, "min_expected_return", "CHOPPY") == 0.004


def test_missing_regime_no_default_fails_closed(caplog) -> None:
    """The BL-4 bug surfaced: map present, regime absent, no default, no
    global used to return None and turn the gate off. It must now fail closed."""
    _reset_warn_dedup()
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01}}  # the prod shape
    with caplog.at_level(logging.WARNING):
        val = T._qp_admission_gate_value(gate, "min_expected_return", "BULL_VOLATILE")
    assert val is T._QP_ADMISSION_MISSING_REGIME
    assert any("failing this admission gate closed" in r.message for r in caplog.records)


def test_fallback_uses_flat_global_when_present(caplog) -> None:
    """When a flat global IS set, the missing regime falls back to it (still
    warned, since that may not be the operator's intent)."""
    _reset_warn_dedup()
    gate = {
        "min_expected_return_by_regime": {"BULL_CALM": 0.01},
        "min_expected_return": 0.002,
    }
    with caplog.at_level(logging.WARNING):
        val = T._qp_admission_gate_value(gate, "min_expected_return", "CHOPPY")
    assert val == 0.002


def test_warning_is_deduped_per_key_regime(caplog) -> None:
    _reset_warn_dedup()
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01}}
    with caplog.at_level(logging.WARNING):
        T._qp_admission_gate_value(gate, "min_expected_return", "BEAR")
        T._qp_admission_gate_value(gate, "min_expected_return", "BEAR")
    warnings = [r for r in caplog.records
                if "failing this admission gate closed" in r.message]
    assert len(warnings) == 1


def test_no_by_regime_map_uses_flat_global() -> None:
    """Unchanged legacy path: no `_by_regime` map → flat global as before."""
    gate = {"min_expected_return": 0.01}
    assert T._qp_admission_gate_value(gate, "min_expected_return", "CHOPPY") == 0.01
    assert T._qp_admission_gate_value(gate, "min_expected_return", None) == 0.01


def test_floor_helper_resolves_via_default(caplog) -> None:
    """End-to-end through the public floor helper: a `default` restores the
    floor in an un-listed regime."""
    _reset_warn_dedup()
    gate = {"min_expected_return_by_regime": {"BULL_CALM": 0.01, "default": 0.006}}
    floor = T._qp_admission_expected_return_floor(gate, is_held=False, regime="BEAR")
    assert floor == 0.006
