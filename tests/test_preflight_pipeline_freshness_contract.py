"""Contract tests for the full preflight pipeline shape."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from renquant_pipeline.kernel.preflight import (
    _LEGACY_CHECK_ORDER,
    PreflightFailed,
    run_preflight,
)
from renquant_pipeline.kernel.preflight_pipeline import (
    PreflightContext,
    build_preflight_pipeline,
)
from renquant_pipeline.kernel.preflight_pipeline.tasks import (
    fundamentals_freshness as ff,
)


def test_full_pipeline_includes_broker_fill_freshness(tmp_path):
    ctx = PreflightContext(config={}, strategy_dir=tmp_path)
    results = build_preflight_pipeline().run(ctx, strict=False)
    names = [r.name for r in results]
    assert len(results) == 21  # +P-CONFIG-SCHEMA, +P-MODEL-STALENESS, +P-FUND-FRESHNESS
    assert "P-KELLY-SIGMA-HORIZON" in names
    assert "P-FUND-FRESHNESS" in names
    assert names[-3:] == [
        "P-STATE-FILE",
        "P-BROKER-CONNECT",
        "P-BROKER-FILL-FRESHNESS",
    ]


def test_run_preflight_legacy_order_covers_all_checks(tmp_path):
    results = run_preflight(config={}, broker=None, strategy_dir=tmp_path, strict=False)
    names = [r.name for r in results]
    assert len(results) == len(_LEGACY_CHECK_ORDER) == 21
    assert names == list(_LEGACY_CHECK_ORDER)
    assert "P-KELLY-SIGMA-HORIZON" in names
    assert "P-FUND-FRESHNESS" in names
    assert "P-BROKER-FILL-FRESHNESS" in names
    assert "P-CONFIG-SCHEMA" in names
    assert "P-MODEL-STALENESS" in names


# ── P-FUND-FRESHNESS through the real run_preflight entrypoint ──────────────
# Verifies the two 2026-06-29 fixes end-to-end at the abort boundary:
#   * a genuinely-stale (quarter-behind) fundamentals panel HARD-fails a
#     full/buy run (still blocks new buys), but is downgraded to a soft pass
#     in sell-only mode (the run does NOT abort).
#   * a true safety-invariant failure (corrupt live_state.json) STILL aborts
#     even in sell-only mode — Fix 2 only exempts buy-only gates.

def _patch_stale_panel(monkeypatch, tmp_path, last_period, today):
    """Make the fundamentals panel a genuinely-stale (quarter-behind) one."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    pd.DataFrame({
        "date": pd.to_datetime([last_period]),
        "ticker": ["AAPL"],
    }).to_parquet(data_dir / "sec_fundamentals_daily.parquet", index=False)
    monkeypatch.setattr(
        "renquant_pipeline.kernel.panel_pipeline._data_root.data_root",
        lambda: tmp_path,
    )

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(ff._dt, "date", _FrozenDate)


def _fund_result(results):
    return next(r for r in results if r.name == "P-FUND-FRESHNESS")


def test_stale_fundamentals_hard_fails_full_run(monkeypatch, tmp_path):
    # Mid-August, panel stuck at Q1 → one quarter behind the filing calendar.
    # In a full/buy run the gate is a HARD failure (blocks new buys).
    _patch_stale_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 8, 16))
    results = run_preflight(config={}, broker=None, strategy_dir=tmp_path,
                            strict=False, run_mode="full")
    fund = _fund_result(results)
    assert fund.severity == "hard" and fund.ok is False
    assert "blocking new buys" in fund.message


def test_stale_fundamentals_does_not_abort_sell_only_run(monkeypatch, tmp_path):
    # Same genuinely-stale panel, but sell-only → P-FUND-FRESHNESS soft pass.
    # The buy-only gate no longer contributes a hard failure, so it cannot
    # by itself abort the sell-only run. (Other gates are config/artifact
    # gates outside this fix's scope; here we assert the freshness gate's
    # own severity, which is what the runner aborts on.)
    _patch_stale_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 8, 16))
    results = run_preflight(config={}, broker=None, strategy_dir=tmp_path,
                            strict=False, run_mode="sell-only (intraday)")
    fund = _fund_result(results)
    assert fund.severity == "soft" and fund.ok is True
    assert "new buys remain blocked" in fund.message
    # Crucially, the freshness gate is NOT in the hard-failure slate now.
    hard_failed = {r.name for r in results if r.severity == "hard" and not r.ok}
    assert "P-FUND-FRESHNESS" not in hard_failed


def test_safety_invariant_state_file_still_aborts_sell_only(monkeypatch, tmp_path):
    # A buy-only gate is exempt in sell-only, but a SAFETY-INVARIANT gate is not.
    # Corrupt live_state.json (state-file integrity) must still HARD-fail and
    # abort even in sell-only mode.
    _patch_stale_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 8, 16))
    (tmp_path / "live_state.paper.json").write_text("{ this is not valid json")
    with pytest.raises(PreflightFailed) as exc:
        run_preflight(config={}, broker=None, strategy_dir=tmp_path,
                      broker_name="paper", strict=True,
                      run_mode="sell-only (intraday)")
    failed = {c.name for c in exc.value.failures}
    assert "P-STATE-FILE" in failed          # safety invariant still aborts
    assert "P-FUND-FRESHNESS" not in failed   # buy-only gate exempted
