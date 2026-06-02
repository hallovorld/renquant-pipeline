"""Contract tests for the 17-check preflight pipeline shape."""
from __future__ import annotations

from renquant_pipeline.kernel.preflight import _LEGACY_CHECK_ORDER, run_preflight
from renquant_pipeline.kernel.preflight_pipeline import (
    PreflightContext,
    build_preflight_pipeline,
)


def test_full_pipeline_includes_broker_fill_freshness(tmp_path):
    ctx = PreflightContext(config={}, strategy_dir=tmp_path)
    results = build_preflight_pipeline().run(ctx, strict=False)
    names = [r.name for r in results]
    assert len(results) == 17
    assert names[-3:] == [
        "P-STATE-FILE",
        "P-BROKER-CONNECT",
        "P-BROKER-FILL-FRESHNESS",
    ]


def test_run_preflight_legacy_order_covers_all_checks(tmp_path):
    results = run_preflight(config={}, broker=None, strategy_dir=tmp_path, strict=False)
    names = [r.name for r in results]
    assert len(results) == len(_LEGACY_CHECK_ORDER) == 17
    assert names == list(_LEGACY_CHECK_ORDER)
    assert "P-BROKER-FILL-FRESHNESS" in names
