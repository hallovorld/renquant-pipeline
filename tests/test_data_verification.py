"""DataVerificationTask — daily-pipeline verification of auxiliary feature feeds.

R2 audit: sec_fundamentals_daily was frozen at 2026-02-10 with no pipeline
check. This stage verifies fundamentals/earnings/sentiment for staleness +
watchlist coverage, warns by default, and fails closed under hard_fail.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel.pipeline.task_data_verification import (
    DataVerificationTask,
    verify_feature_data_sources,
)


def _make_data_root(tmp_path: Path, fund_max="2026-06-10", sent_max="2026-06-10",
                    tickers=("AAA", "BBB", "CCC")) -> Path:
    data = tmp_path / "data"
    (data).mkdir(parents=True, exist_ok=True)
    # fundamentals single parquet
    rows = []
    for t in tickers:
        rows.append({"ticker": t, "date": pd.Timestamp(fund_max),
                     "earnings_yield": 0.05})
    pd.DataFrame(rows).to_parquet(data / "sec_fundamentals_daily.parquet")
    # sentiment per-ticker dir
    sdir = data / "news_sentiment_alpaca"
    sdir.mkdir()
    for t in tickers:
        pd.DataFrame({"date": [pd.Timestamp(sent_max)], "mean_sentiment": [0.1]}
                     ).to_parquet(sdir / f"{t}.parquet")
    # earnings per-ticker dir
    edir = data / "earnings_surprise"
    edir.mkdir()
    for t in tickers:
        pd.DataFrame({"earnings_date": [pd.Timestamp("2026-05-01")],
                      "sue_signal": [0.3]}).to_parquet(edir / f"{t}.parquet")
    return tmp_path


def test_fresh_feeds_all_ok(tmp_path) -> None:
    root = _make_data_root(tmp_path)
    rep = verify_feature_data_sources(
        root, ["AAA", "BBB", "CCC"], pd.Timestamp("2026-06-11"), {})
    assert rep["fundamentals"]["ok"] and rep["fundamentals"]["stale_days"] == 1
    assert rep["sentiment"]["ok"]
    assert rep["earnings"]["ok"] and rep["earnings"]["coverage"] == 1.0


def test_stale_fundamentals_flagged(tmp_path) -> None:
    """The exact BLOCKER: fundamentals frozen 4 months back."""
    root = _make_data_root(tmp_path, fund_max="2026-02-10")
    rep = verify_feature_data_sources(
        root, ["AAA", "BBB", "CCC"], pd.Timestamp("2026-06-11"), {})
    f = rep["fundamentals"]
    assert not f["ok"]
    assert f["stale_days"] > 100
    assert any("stale" in r for r in f["reasons"])


def test_low_coverage_flagged(tmp_path) -> None:
    root = _make_data_root(tmp_path, tickers=("AAA",))  # only 1 of 3 covered
    rep = verify_feature_data_sources(
        root, ["AAA", "BBB", "CCC"], pd.Timestamp("2026-06-11"), {})
    assert rep["fundamentals"]["coverage"] == pytest.approx(1 / 3)
    assert not rep["fundamentals"]["ok"]  # below 0.80 default


def test_missing_feed_flagged(tmp_path) -> None:
    (tmp_path / "data").mkdir()  # empty data dir, no parquets
    rep = verify_feature_data_sources(
        tmp_path, ["AAA"], pd.Timestamp("2026-06-11"), {})
    assert not rep["fundamentals"]["present"]
    assert any("missing" in r for r in rep["fundamentals"]["reasons"])


def test_task_warns_by_default_does_not_raise(tmp_path, monkeypatch) -> None:
    root = _make_data_root(tmp_path, fund_max="2026-02-10")
    monkeypatch.setattr(
        "renquant_pipeline.kernel.pipeline.task_data_verification.data_root",
        lambda: root)
    ctx = SimpleNamespace(
        config={"watchlist": ["AAA", "BBB", "CCC"],
                "data_verification": {"enabled": True}},
        today=pd.Timestamp("2026-06-11"), counters={})
    # must NOT raise (default warn)
    assert DataVerificationTask().run(ctx) is True
    assert ctx._data_verification["fundamentals"]["ok"] is False
    assert ctx.counters["data_verification_failures"] >= 1


def test_task_hard_fail_raises(tmp_path, monkeypatch) -> None:
    root = _make_data_root(tmp_path, fund_max="2026-02-10")
    monkeypatch.setattr(
        "renquant_pipeline.kernel.pipeline.task_data_verification.data_root",
        lambda: root)
    ctx = SimpleNamespace(
        config={"watchlist": ["AAA", "BBB", "CCC"],
                "data_verification": {"enabled": True, "hard_fail": True}},
        today=pd.Timestamp("2026-06-11"), counters={})
    with pytest.raises(RuntimeError, match="failed verification"):
        DataVerificationTask().run(ctx)


def test_task_disabled_skips(tmp_path, monkeypatch) -> None:
    ctx = SimpleNamespace(
        config={"data_verification": {"enabled": False}},
        today=pd.Timestamp("2026-06-11"), counters={})
    assert DataVerificationTask().run(ctx) is True
    assert not hasattr(ctx, "_data_verification")
