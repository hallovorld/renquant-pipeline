"""P-FUND-FRESHNESS: fail-closed preflight gate on stale fundamental data.

2026-06-23 incident: ``sec_fundamentals_daily.parquet`` silently went 91 days
stale (last row 2026-03-24) and fed live scoring + trading with no preflight
signal, because price/sentiment were fresh. This gate makes fundamental-panel
staleness a first-class preflight control.

2026-06-29 fix — SPLIT two distinct freshness concepts that an earlier draft
conflated into a single ``date``-column check:

  ``sec_fundamentals_daily.parquet`` is BOTH a daily forward-filled feed AND a
  carrier of quarterly fiscal snapshots. Its ``date`` column is an as-of / feed
  date (2,575 distinct daily dates 2016-01-04 → 2026-03-31), and price-dependent
  fields like ``book_to_price`` vary day by day — so a fresh feed must be roughly
  CURRENT. But the underlying fundamentals only advance once per fiscal quarter,
  and the SEC 10-Q for a quarter is not filed until ~40-45 days AFTER the period
  end, so during the normal mid-quarter filing gap the latest fiscal snapshot is
  legitimately the PREVIOUS quarter.

  The earlier draft replaced the as-of staleness check with a quarterly filing
  calendar. That HID a genuinely-stopped feed: a feed forward-filled out to a
  current ``date`` while the fiscal snapshot stays old would pass the calendar
  check, and a feed whose max ``date`` is 90 days old would be labelled "fresh"
  as long as its quarter still matched the calendar. Both are false negatives
  for the stopped-refresh class the original guard was meant to surface.

  The fix keeps BOTH dimensions as independent checks; the gate fails (buy-side)
  if EITHER trips:

    1. DAILY-FEED freshness (``feed_age_days = today - feed_max_date``). The feed
       is daily forward-filled and must stay ~current; ``feed_age_days`` past
       ``max_feed_stale_days`` (default 20, aligned with ``DataVerificationTask``
       and ``job_panel_scoring._FUND_STALE_WARN_DAYS=15``) means the daily
       refresh has stopped — a real problem. THIS is what catches the
       2026-06-23 incident and the current 2026-03-31 feed (~90d old on
       2026-06-29). HARD buy-block by default; do not silently weaken it.

    2. QUARTERLY-FILING availability (the filing-calendar heuristic). Validates
       that the panel's latest fiscal quarter is at/beyond the
       latest-expected-filed quarter (10-Q deadline + ingest lag). Catches a
       feed whose daily ``date`` keeps advancing (so dimension 1 passes) while
       the fiscal snapshot is stuck one or more quarters behind the calendar.
       This is an EXPECTED-AVAILABILITY heuristic, NOT a daily-freshness
       statement, and it does not mask dimension 1.

  Data limitation: the parquet exposes no true filing-date / fiscal-period-date
  column (only the as-of ``date``), so dimension 2 can only assert a coarse
  quarterly-availability heuristic by snapping the as-of date to its calendar
  quarter. Both facts (``feed_max_date`` / ``feed_age_days`` and
  ``panel_quarter`` / ``expected_filed_quarter``) are reported so operators see
  the daily and quarterly pictures separately and are not told a 90d-old feed is
  simply "fresh".

Sell-only fix (2026-06-29): P-FUND-FRESHNESS is a BUY-ONLY gate — its message
says "blocking new buys". A sell-only intraday run places no buys, so a
stale-fundamentals finding has no bearing on it; the gate must NOT abort the
sell path. Like every other buy-only gate (P-SECTOR-MAP, P-CONFIG-FP, P-WF-GATE,
…), it routes through ``_soft_for_sell_only`` so sell-only runs downgrade it to a
logged soft pass while full/buy runs still hard-fail and block new buys.

Config (``preflight.fundamentals_freshness``):
  - enabled             (default True)
  - max_feed_stale_days (default 20) — DAILY-FEED dimension: how many calendar
    days the feed's max as-of ``date`` may lag today before the daily refresh is
    judged stopped/stale. Aligned with ``DataVerificationTask``'s fundamentals
    ``max_stale_days=20``; configurable independently of the quarterly gate.
  - filing_lag_days     (default 45) — QUARTERLY dimension: days after a
    fiscal-period end at which that quarter's 10-Q is "expected filed /
    available". SEC 10-Q deadlines are 40d (large accelerated) / 45d
    (accelerated / non-accelerated); 45 is the conservative ceiling, and our
    daily refresh lands the data within a day or two of the filing.
  - max_quarters_behind (default 1) — QUARTERLY dimension: how many fiscal
    quarters the panel may lag the latest-expected-filed quarter before that
    dimension trips. 1 == "must be at the latest expected quarter".
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from renquant_pipeline.kernel.preflight import (  # noqa: PLC0415 (legacy bridge)
    PreflightCheck,
    _soft_for_sell_only,
)

from ..base import PreflightTask
from ..ctx import PreflightContext

# DAILY-FEED dimension: calendar-day age of the feed's max as-of ``date`` beyond
# which the daily forward-filled refresh is judged stopped/stale. Aligned with
# ``DataVerificationTask`` fundamentals ``max_stale_days=20`` and the
# ``job_panel_scoring._FUND_STALE_WARN_DAYS=15`` daily-feed warning.
_DEFAULT_MAX_FEED_STALE_DAYS = 20
# QUARTERLY dimension: days after a fiscal-period end at which that quarter's
# 10-Q is expected to be filed AND ingested. SEC 10-Q deadlines: 40d (large
# accelerated filers) / 45d (accelerated + non-accelerated). 45 is the
# conservative ceiling; our refresh job lands the data within a day or two of
# the EDGAR filing.
_DEFAULT_FILING_LAG_DAYS = 45
# QUARTERLY dimension: how many fiscal quarters the panel may lag the
# latest-expected-filed quarter before that dimension trips. 1 == "must be at
# the latest expected quarter".
_DEFAULT_MAX_QUARTERS_BEHIND = 1

# Fiscal-quarter period-end month/day (calendar quarters; matches the SEC
# period_end convention used by ``sec_fundamentals_daily.parquet``).
_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


def _fund_max_date(path: Path):
    if not path.exists():
        return None
    import pandas as pd  # noqa: PLC0415

    df = pd.read_parquet(path, columns=["date"])
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    return s.max().date() if len(s) else None


def feed_age_days(feed_max_date, today: _dt.date):
    """Calendar-day age of the feed's max as-of ``date`` vs ``today``.

    ``feed_max_date is None`` (missing/empty panel) → ``None`` (caller skips).
    """
    if feed_max_date is None:
        return None
    return max(0, (today - feed_max_date).days)


def _recent_quarter_ends(today: _dt.date, lookback_quarters: int = 12):
    """Fiscal quarter-end dates at or before ``today``, newest first."""
    ends: list[_dt.date] = []
    for year in range(today.year, today.year - (lookback_quarters // 4 + 2), -1):
        for month, day in reversed(_QUARTER_ENDS):
            qe = _dt.date(year, month, day)
            if qe <= today:
                ends.append(qe)
    ends.sort(reverse=True)
    return ends[:lookback_quarters]


def _snap_to_quarter(d: _dt.date) -> _dt.date:
    """Snap a date back to the most recent fiscal quarter-end at or before it."""
    for month, day in reversed(_QUARTER_ENDS):
        qe = _dt.date(d.year, month, day)
        if qe <= d:
            return qe
    return _dt.date(d.year - 1, 12, 31)


def latest_expected_filed_quarter(today: _dt.date, filing_lag_days: int):
    """Most recent fiscal quarter whose 10-Q is expected filed by ``today``.

    A quarter ending on ``qe`` is "expected available" once ``filing_lag_days``
    have elapsed since ``qe`` (the 10-Q deadline + ingest lag). Returns the
    newest such quarter-end, or ``None`` if none qualify (degenerate).
    """
    for qe in _recent_quarter_ends(today):
        if (today - qe).days >= filing_lag_days:
            return qe
    return None


def quarters_behind(panel_max_date, today: _dt.date, filing_lag_days: int):
    """How many fiscal quarters the panel lags the latest-expected-filed quarter.

    Returns ``(n_behind, expected_quarter, panel_quarter)`` where ``n_behind`` is
    0 when the panel is at/ahead of the expected quarter (fresh), and >= 1 when
    it is behind (the broken-refresh signature). ``panel_max_date is None`` and
    a degenerate calendar both yield ``None`` for ``n_behind`` (caller skips).
    """
    expected = latest_expected_filed_quarter(today, filing_lag_days)
    if panel_max_date is None or expected is None:
        return None, expected, panel_max_date
    # A forward-filled panel can carry a daily ``date`` past the period end, so
    # snap both sides to their fiscal quarter before counting.
    expected_q = _snap_to_quarter(expected)
    panel_q = _snap_to_quarter(panel_max_date)
    if panel_q >= expected_q:
        return 0, expected_q, panel_q
    # Count quarter-ends strictly between the panel's latest period and the
    # expected quarter (inclusive of the expected quarter).
    n = 0
    for qe in _recent_quarter_ends(today):
        if qe <= panel_q:
            break
        if qe <= expected_q:
            n += 1
    return n, expected_q, panel_q


def classify_freshness(panel_max_date, today: _dt.date, filing_lag_days: int,
                       max_quarters_behind: int = _DEFAULT_MAX_QUARTERS_BEHIND,
                       max_feed_stale_days: int = _DEFAULT_MAX_FEED_STALE_DAYS):
    """Pure decision → (ok, message, details). ``panel_max_date is None`` → skip.

    Evaluates TWO independent dimensions and FAILS (not ok) if EITHER trips:

      * DAILY-FEED: ``feed_age_days`` (today - feed max as-of date) must be within
        ``max_feed_stale_days``; otherwise the daily forward-filled refresh has
        stopped (a real problem — this is what catches a 90d-old feed).
      * QUARTERLY: the panel's fiscal quarter must be at/beyond the
        latest-expected-filed quarter; otherwise the fiscal snapshot is stuck
        one or more quarters behind the filing calendar.

    PASS only when BOTH are satisfied. Both facts are always reported in
    ``details`` and surfaced in ``message`` so neither dimension masks the other.
    """
    age = feed_age_days(panel_max_date, today)
    n_behind, expected_q, panel_q = quarters_behind(
        panel_max_date, today, filing_lag_days)
    details = {
        "feed_max_date": panel_max_date.isoformat() if panel_max_date else None,
        "feed_age_days": age,
        "max_feed_stale_days": max_feed_stale_days,
        "panel_quarter": panel_q.isoformat() if isinstance(panel_q, _dt.date) else None,
        "expected_filed_quarter": (
            expected_q.isoformat() if isinstance(expected_q, _dt.date) else None),
        "quarters_behind": n_behind,
        "filing_lag_days": filing_lag_days,
        "max_quarters_behind": max_quarters_behind,
    }
    if panel_max_date is None:
        return True, "no fundamentals panel; skip", details

    feed_stale = age is not None and age >= max_feed_stale_days
    quarter_behind = n_behind is not None and n_behind >= max_quarters_behind

    # Always report both facts, regardless of which (if any) tripped, so a stale
    # daily feed is never labelled simply "fresh".
    feed_fact = (
        f"daily feed as-of {panel_max_date.isoformat()} is {age}d old "
        f"(max_feed_stale_days={max_feed_stale_days})")
    quarter_fact = (
        f"panel fiscal quarter {panel_q.isoformat()} vs latest-expected-filed "
        f"{expected_q.isoformat()} ({n_behind} quarter(s) behind, 10-Q lag "
        f"{filing_lag_days}d)")
    note = (
        "note: the parquet has no true filing/period-date column, so the "
        "quarterly check is a coarse as-of-snapped availability heuristic")

    if feed_stale or quarter_behind:
        reasons = []
        if feed_stale:
            reasons.append(f"DAILY-FEED STALE — {feed_fact}: the daily "
                           "forward-filled refresh appears stopped")
        if quarter_behind:
            reasons.append(f"QUARTERLY SNAPSHOT BEHIND — {quarter_fact}: a "
                           "stuck/broken fiscal refresh, not the normal "
                           "mid-quarter filing gap")
        return (False,
                f"fundamentals stale — blocking new buys. "
                f"{' | '.join(reasons)}. {note}",
                details)

    return (True,
            f"fundamentals fresh — daily feed {feed_fact}; quarterly "
            f"{quarter_fact}; the next quarter is not yet expected to be filed. "
            f"{note}",
            details)


class FundamentalsFreshnessTask(PreflightTask):
    """P-FUND-FRESHNESS: daily feed current AND fiscal snapshot not quarter-behind.

    Two independent dimensions (daily-feed as-of freshness + quarterly filing
    availability); fails if EITHER trips. Buy-only gate: a stale-fundamentals
    finding blocks new buys but is exempt in sell-only runs (no buys to block),
    routed through ``_soft_for_sell_only``.
    """

    check_name = "P-FUND-FRESHNESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        cfg = (((ctx.config or {}).get("preflight", {}) or {})
               .get("fundamentals_freshness", {}) or {})
        if not cfg.get("enabled", True):
            return PreflightCheck(self.check_name, "soft", True, "disabled; skip")
        filing_lag = int(cfg.get("filing_lag_days", _DEFAULT_FILING_LAG_DAYS))
        max_behind = int(cfg.get("max_quarters_behind", _DEFAULT_MAX_QUARTERS_BEHIND))
        max_feed_stale = int(
            cfg.get("max_feed_stale_days", _DEFAULT_MAX_FEED_STALE_DAYS))
        try:
            from renquant_pipeline.kernel.panel_pipeline._data_root import (  # noqa: PLC0415
                data_root,
            )
            path = data_root() / "data" / "sec_fundamentals_daily.parquet"
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(self.check_name, "soft", True,
                                  f"data_root unavailable: {exc}; skip")
        panel_max = _fund_max_date(path)
        ok, msg, details = classify_freshness(
            panel_max, _dt.date.today(), filing_lag, max_behind, max_feed_stale)
        details["path"] = str(path)
        if ok:
            return PreflightCheck(self.check_name, "hard", True, msg, details)
        # Stale (daily feed and/or quarterly snapshot): HARD buy-block in
        # full/buy mode; soft pass in sell-only mode (sell-only places no buys,
        # so a buy-side staleness gate must not abort the risk-exit path).
        # Mirrors every other buy-only preflight gate.
        return _soft_for_sell_only(
            self.check_name, msg, run_mode=ctx.run_mode, details=details)
