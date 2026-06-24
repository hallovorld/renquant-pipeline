"""Tests for the P-FUND-FRESHNESS preflight gate."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from renquant_pipeline.kernel.preflight_pipeline.tasks.fundamentals_freshness import (
    classify_freshness,
    fund_age_days,
)


# ── classify_freshness (pure severity decision) ───────────────────────────
@pytest.mark.parametrize("age,exp", [
    (0,  ("hard", True)),
    (29, ("hard", True)),
    (30, ("soft", False)),   # warn band
    (44, ("soft", False)),
    (45, ("hard", False)),   # critical → HARD fail (block new buys)
    (91, ("hard", False)),   # the 2026-06-23 incident
])
def test_classify_severity_and_ok(age, exp):
    sev, ok, _ = classify_freshness(age, warn=30, critical=45)
    assert (sev, ok) == exp


def test_classify_none_is_soft_skip():
    assert classify_freshness(None, 30, 45) == ("soft", True, "no fundamentals panel; skip")


def test_classify_critical_message_mentions_blocking():
    _, _, msg = classify_freshness(91, 30, 45)
    assert "blocking new buys" in msg


# ── fund_age_days ─────────────────────────────────────────────────────────
def _write(path, last):
    pd.DataFrame({"date": pd.to_datetime([last - pd.Timedelta(days=1), last]),
                  "ticker": ["AAPL", "AAPL"]}).to_parquet(path, index=False)


def test_fund_age_days_from_parquet(tmp_path):
    p = tmp_path / "sec_fundamentals_daily.parquet"
    _write(p, pd.Timestamp("2026-03-24"))
    assert fund_age_days(p, date(2026, 6, 23)) == 91  # the incident age


def test_fund_age_days_missing_file(tmp_path):
    assert fund_age_days(tmp_path / "nope.parquet", date(2026, 6, 23)) is None


def test_fund_age_days_fresh(tmp_path):
    p = tmp_path / "sec_fundamentals_daily.parquet"
    _write(p, pd.Timestamp("2026-06-23"))
    assert fund_age_days(p, date(2026, 6, 23)) == 0
