"""Tests for the P-FUND-FRESHNESS preflight gate.

Covers the 2026-06-29 fix, which SPLITS two distinct freshness concepts that an
earlier draft had conflated into one ``date``-column check:

  * Dimension 1 — DAILY-FEED as-of freshness: the daily forward-filled parquet
    must stay ~current. A feed whose max as-of ``date`` is >= ``max_feed_stale_days``
    old is flagged STALE (the stopped-refresh signature) even if its quarter
    still matches the filing calendar. THIS is the check the earlier draft
    dropped, which would have hidden the real 2026-03-31-on-2026-06-29 stopped
    feed.
  * Dimension 2 — QUARTERLY filing availability: freshness measured against the
    quarterly FILING calendar, not a fixed day-count. Q1 (Mar 31) in late June
    is the latest-expected-filed quarter → not quarter-behind; a snapshot stuck
    multiple quarters behind the calendar → behind.
  * Sell-only — a sell-only run is NOT aborted by this buy-only gate (downgrades
    to a logged soft pass), while a full/buy run still hard-fails and blocks
    buys. Safety-invariant gates are unaffected (covered elsewhere).
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from renquant_pipeline.kernel.preflight import PreflightCheck
from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks import fundamentals_freshness as ff
from renquant_pipeline.kernel.preflight_pipeline.tasks.fundamentals_freshness import (
    FundamentalsFreshnessTask,
    _fund_max_date,
    classify_freshness,
    feed_age_days,
    latest_expected_filed_quarter,
    quarters_behind,
)

_LAG = 45  # default filing_lag_days
_FEED = 20  # default max_feed_stale_days


# ── Dimension 1: daily-feed as-of age ──────────────────────────────────────
def test_feed_age_days_basic():
    assert feed_age_days(date(2026, 3, 31), date(2026, 6, 29)) == 90
    assert feed_age_days(date(2026, 6, 29), date(2026, 6, 29)) == 0


def test_feed_age_days_none_panel():
    assert feed_age_days(None, date(2026, 6, 29)) is None


# ── Dimension 2: latest-expected-filed-quarter calendar ────────────────────
@pytest.mark.parametrize("today,expected", [
    # Late June 2026 (the incident date): Q1 (Mar 31) filed ~mid-May → expected.
    (date(2026, 6, 29), date(2026, 3, 31)),
    # Mid-May, BEFORE Q1's ~45d filing window closes: Q4-prior is the latest
    # expected, Q1 not yet due. This is the normal mid-quarter filing gap.
    (date(2026, 5, 10), date(2025, 12, 31)),
    # Just AFTER Q1's filing window: Q1 becomes expected.
    (date(2026, 5, 20), date(2026, 3, 31)),
    # Just AFTER Q2's (Jun 30) filing window in mid-August: Q2 expected.
    (date(2026, 8, 16), date(2026, 6, 30)),
])
def test_latest_expected_filed_quarter(today, expected):
    assert latest_expected_filed_quarter(today, _LAG) == expected


def test_quarters_behind_fresh_when_at_expected_quarter():
    # Panel at Q1 (Mar 31), today late June → expected is Q1 → 0 behind.
    n, exp_q, panel_q = quarters_behind(date(2026, 3, 31), date(2026, 6, 29), _LAG)
    assert n == 0
    assert exp_q == date(2026, 3, 31)
    assert panel_q == date(2026, 3, 31)


def test_quarters_behind_forward_filled_panel_snaps_to_quarter():
    # A forward-filled daily panel can carry a date past the period end (e.g.
    # 2026-04-15). It still represents Q1 fundamentals → 0 behind, not "ahead".
    n, exp_q, panel_q = quarters_behind(date(2026, 4, 15), date(2026, 6, 29), _LAG)
    assert n == 0
    assert panel_q == date(2026, 3, 31)


def test_quarters_behind_one_quarter_stuck_is_behind():
    # Mid-August: Q2 expected, panel stuck at Q1 (Mar 31) → 1 quarter behind.
    n, exp_q, panel_q = quarters_behind(date(2026, 3, 31), date(2026, 8, 16), _LAG)
    assert n == 1
    assert exp_q == date(2026, 6, 30)


def test_quarters_behind_multiple_quarters_broken_refresh():
    # The 2026-06-23 incident class taken further: panel stuck at Q3-2025
    # (Sep 30 2025) while late-June expects Q1-2026 → 2 quarters behind.
    n, exp_q, panel_q = quarters_behind(date(2025, 9, 30), date(2026, 6, 29), _LAG)
    assert n == 2
    assert exp_q == date(2026, 3, 31)


# ── classify_freshness (pure decision, BOTH dimensions) ────────────────────
def test_classify_current_feed_at_latest_quarter_is_fresh():
    # Feed as-of today AND panel at the latest-expected quarter → fresh.
    ok, msg, details = classify_freshness(date(2026, 6, 29), date(2026, 6, 29),
                                          _LAG, max_feed_stale_days=_FEED)
    assert ok is True
    assert details["feed_age_days"] == 0
    assert details["quarters_behind"] == 0
    assert "fresh" in msg


def test_classify_stopped_feed_fails_even_if_quarter_matches_calendar():
    # THE Codex failure mode — the live 2026-06-29 case: max as-of date is
    # 2026-03-31 (~90d old). The quarterly calendar says Q1 is the latest
    # expected (0 quarters behind), but the DAILY FEED is 90d > 20d stale, so
    # the gate MUST still FAIL. A stopped daily feed is NOT "fresh". The earlier
    # draft's calendar-only logic would have passed this and hidden the stop.
    today = date(2026, 6, 29)
    ok, msg, details = classify_freshness(date(2026, 3, 31), today, _LAG,
                                          max_feed_stale_days=_FEED)
    assert ok is False
    assert details["feed_age_days"] == 90
    assert details["quarters_behind"] == 0  # quarterly dimension alone passes
    assert "DAILY-FEED STALE" in msg
    assert "blocking new buys" in msg
    # Both facts visible: the as-of date AND the (passing) quarter.
    assert details["feed_max_date"] == "2026-03-31"
    assert details["expected_filed_quarter"] == "2026-03-31"


def test_classify_forward_filled_to_current_date_is_fresh_on_both():
    # A feed forward-filled out to a CURRENT as-of date satisfies both the daily
    # dimension (0d old) and the quarterly calendar (snap(2026-08-16)=Q2 == the
    # latest-expected quarter) → genuinely fresh, which is correct. The point of
    # the daily dimension is that the moment such a feed STOPS advancing (its
    # as-of date freezes) the daily age check catches it — see the stopped-feed
    # regression above.
    ok, _, details = classify_freshness(date(2026, 8, 16), date(2026, 8, 16),
                                        _LAG, max_feed_stale_days=_FEED)
    assert ok is True
    assert details["feed_age_days"] == 0
    assert details["quarters_behind"] == 0


def test_classify_quarter_behind_also_trips_quarterly_dimension():
    # Mid-August, panel stuck at Q1: it is BOTH daily-stale (>> 20d) AND one
    # quarter behind the filing calendar. Both reasons are reported.
    ok, msg, details = classify_freshness(date(2026, 3, 31), date(2026, 8, 16),
                                          _LAG, max_feed_stale_days=_FEED)
    assert ok is False
    assert details["quarters_behind"] == 1
    assert "QUARTERLY SNAPSHOT BEHIND" in msg
    assert "DAILY-FEED STALE" in msg  # also stale at the daily level


def test_classify_broken_refresh_multiple_quarters_behind():
    ok, msg, details = classify_freshness(date(2025, 9, 30), date(2026, 6, 29),
                                          _LAG, max_feed_stale_days=_FEED)
    assert ok is False
    assert details["quarters_behind"] == 2
    assert "QUARTERLY SNAPSHOT BEHIND" in msg


def test_classify_reports_both_facts_and_data_limitation():
    # Operators must see feed_max_date, feed_age_days, expected_filed_quarter,
    # and the data-limitation note in every non-skip outcome.
    ok, msg, details = classify_freshness(date(2026, 3, 31), date(2026, 6, 29),
                                          _LAG, max_feed_stale_days=_FEED)
    for key in ("feed_max_date", "feed_age_days", "expected_filed_quarter",
                "panel_quarter", "quarters_behind", "max_feed_stale_days"):
        assert key in details
    assert "no true filing/period-date column" in msg


def test_classify_none_panel_is_skip():
    ok, msg, details = classify_freshness(None, date(2026, 6, 29), _LAG,
                                          max_feed_stale_days=_FEED)
    assert ok is True
    assert msg == "no fundamentals panel; skip"
    assert details["feed_age_days"] is None


# ── _fund_max_date ─────────────────────────────────────────────────────────
def _write(path, last):
    pd.DataFrame({"date": pd.to_datetime([last - pd.Timedelta(days=1), last]),
                  "ticker": ["AAPL", "AAPL"]}).to_parquet(path, index=False)


def test_fund_max_date_from_parquet(tmp_path):
    p = tmp_path / "sec_fundamentals_daily.parquet"
    _write(p, pd.Timestamp("2026-03-31"))
    assert _fund_max_date(p) == date(2026, 3, 31)


def test_fund_max_date_missing_file(tmp_path):
    assert _fund_max_date(tmp_path / "nope.parquet") is None


# ── Task: sell-only must NOT abort on this buy-only gate ────────────────────
def _patch_panel(monkeypatch, tmp_path, last_period, today):
    """Point the task at a tmp parquet with the given latest as-of date + freeze today."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    p = data_dir / "sec_fundamentals_daily.parquet"
    _write(p, pd.Timestamp(last_period))
    # data_root() returns the umbrella root; the task appends data/<file>.
    monkeypatch.setattr(
        "renquant_pipeline.kernel.panel_pipeline._data_root.data_root",
        lambda: tmp_path,
    )

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(ff._dt, "date", _FrozenDate)


def test_task_stopped_feed_full_run_hard_fails_blocks_buys(monkeypatch, tmp_path):
    # The live 2026-06-29 case: feed max as-of 2026-03-31 (~90d old) → daily
    # feed stale → HARD fail, even though the quarterly calendar is satisfied.
    _patch_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 6, 29))
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    res = FundamentalsFreshnessTask().check(ctx)
    assert isinstance(res, PreflightCheck)
    assert res.severity == "hard"
    assert res.ok is False
    assert "DAILY-FEED STALE" in res.message
    assert "blocking new buys" in res.message
    assert res.details["feed_age_days"] == 90
    assert res.details["quarters_behind"] == 0


def test_task_stopped_feed_sell_only_run_does_not_abort(monkeypatch, tmp_path):
    # Same stopped feed, but sell-only → soft pass (run proceeds).
    _patch_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 6, 29))
    ctx = PreflightContext(config={}, strategy_dir=tmp_path,
                           run_mode="sell-only (intraday)")
    res = FundamentalsFreshnessTask().check(ctx)
    assert res.severity == "soft"
    assert res.ok is True  # would NOT contribute to PreflightFailed → no abort
    assert "new buys remain blocked" in res.message


def test_task_quarter_behind_full_run_hard_fails(monkeypatch, tmp_path):
    # Mid-August, panel stuck at Q1 → both daily-stale and one quarter behind.
    _patch_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 8, 16))
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    res = FundamentalsFreshnessTask().check(ctx)
    assert res.severity == "hard"
    assert res.ok is False
    assert "QUARTERLY SNAPSHOT BEHIND" in res.message
    assert res.details["quarters_behind"] == 1


def test_task_fresh_current_feed_passes_in_full_run(monkeypatch, tmp_path):
    # Feed as-of today AND at the latest-expected quarter → fresh → PASS.
    _patch_panel(monkeypatch, tmp_path, "2026-06-29", date(2026, 6, 29))
    ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
    res = FundamentalsFreshnessTask().check(ctx)
    assert res.severity == "hard"
    assert res.ok is True
    assert res.details["feed_age_days"] == 0
    assert res.details["quarters_behind"] == 0


def test_task_max_feed_stale_days_independently_configurable(monkeypatch, tmp_path):
    # The 90d-old feed passes the daily dimension only if the operator widens
    # max_feed_stale_days beyond it — confirming the daily check is configurable
    # independently of the quarterly gate (here the quarter still passes).
    _patch_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 6, 29))
    ctx = PreflightContext(
        config={"preflight": {"fundamentals_freshness": {
            "max_feed_stale_days": 120}}},
        strategy_dir=tmp_path, run_mode="full")
    res = FundamentalsFreshnessTask().check(ctx)
    assert res.ok is True
    assert res.details["max_feed_stale_days"] == 120
    assert res.details["feed_age_days"] == 90


def test_task_disabled_is_soft_skip(monkeypatch, tmp_path):
    _patch_panel(monkeypatch, tmp_path, "2026-03-31", date(2026, 8, 16))
    ctx = PreflightContext(
        config={"preflight": {"fundamentals_freshness": {"enabled": False}}},
        strategy_dir=tmp_path, run_mode="full")
    res = FundamentalsFreshnessTask().check(ctx)
    assert res.ok is True
    assert res.severity == "soft"
