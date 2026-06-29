"""P-FUND-FRESHNESS: fail-closed preflight gate on stale fundamental data.

2026-06-23 incident: ``sec_fundamentals_daily.parquet`` silently went 91 days
stale (last row 2026-03-24) and fed live scoring + trading with no preflight
signal, because price/sentiment were fresh. This gate makes fundamental-panel
staleness a first-class preflight control.

2026-06-29 fix — measure against the quarterly FILING CALENDAR, not a fixed
day-count from the period date:

  The original gate computed ``age = today - latest_period_date`` and hard-failed
  at ``age >= 45``. But quarterly fundamentals only advance once per quarter, at
  the fiscal-period end. The SEC 10-Q for a quarter is not filed until ~40-45
  days AFTER the period end, and our refresh job ingests it a few days later. So
  for the NORMAL gap between one quarter's filing and the next quarter's filing,
  the latest available fundamental period is the PREVIOUS quarter — by design.

  Example (today 2026-06-29): the latest period in the panel is 2026-03-31
  (Q1, fiscal period end). Q2 (period end 2026-06-30) cannot be filed until
  ~mid-August. With the old fixed-45d rule, ``age = 90`` → HARD fail and ALL
  new buys were blocked, even though Q1 IS the freshest fundamental data that
  can possibly exist right now. This blocked new buys for roughly HALF of every
  quarter (the filing-gap window), not because data was broken but because the
  threshold ignored the filing calendar.

  The fix: compute the LATEST-EXPECTED-FILED fiscal quarter for today using the
  10-Q deadline calendar — a quarter's data is "expected to be available" once
  ``filing_lag_days`` (default 45) have elapsed since its period end. PASS when
  the panel's latest period is at or beyond that quarter; HARD-fail (buy-side)
  when the panel is one or more quarters BEHIND the calendar, which is the
  genuine broken-refresh signature (the 2026-06-23 incident: stuck at a quarter
  the calendar says should long since have rolled forward).

  This still catches a real broken refresh: if the refresh job dies and the
  panel sits at Q4-prior while the calendar expects Q1, the panel is one quarter
  behind → HARD fail. Only the *normal* mid-quarter filing gap is now exempt.

Sell-only fix (2026-06-29): P-FUND-FRESHNESS is a BUY-ONLY gate — its own
message says "blocking new buys". A sell-only intraday run places no buys, so a
stale-fundamentals finding has no bearing on it; the gate must NOT abort the
sell path. Like every other buy-only gate (P-SECTOR-MAP, P-CONFIG-FP, P-WF-GATE,
…), it now routes through ``_soft_for_sell_only`` so sell-only runs downgrade it
to a logged soft pass while full/buy runs still hard-fail and block new buys.

Config (``preflight.fundamentals_freshness``):
  - enabled         (default True)
  - filing_lag_days (default 45) — days after a fiscal-period end at which that
    quarter's 10-Q is "expected filed / available". SEC 10-Q deadlines are
    40d (large accelerated) / 45d (accelerated / non-accelerated); 45 is the
    conservative ceiling, and our daily refresh lands the data within a day or
    two of the filing. Bump this only if the data vendor lags the EDGAR filing.
  - max_quarters_behind (default 1) — how many fiscal quarters the panel may lag
    the latest-expected-filed quarter before the gate HARD-fails. 1 == "must be
    at the latest expected quarter".
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

# Days after a fiscal-period end at which that quarter's 10-Q is expected to be
# filed AND ingested. SEC 10-Q deadlines: 40d (large accelerated filers) /
# 45d (accelerated + non-accelerated). 45 is the conservative ceiling; our
# refresh job lands the data within a day or two of the EDGAR filing.
_DEFAULT_FILING_LAG_DAYS = 45
# How many fiscal quarters the panel may lag the latest-expected-filed quarter
# before the gate hard-fails. 1 == "must be at the latest expected quarter".
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
                       max_quarters_behind: int = _DEFAULT_MAX_QUARTERS_BEHIND):
    """Pure decision → (ok, message, details). ``panel_max_date is None`` → skip.

    PASS when the panel is at/beyond the latest-expected-filed quarter (the
    normal mid-quarter filing gap is fresh, by design). FAIL when the panel is
    ``max_quarters_behind`` or more quarters behind the calendar — the genuine
    broken-refresh signature.
    """
    n_behind, expected_q, panel_q = quarters_behind(
        panel_max_date, today, filing_lag_days)
    details = {
        "panel_max_date": panel_max_date.isoformat() if panel_max_date else None,
        "panel_quarter": panel_q.isoformat() if isinstance(panel_q, _dt.date) else None,
        "expected_filed_quarter": (
            expected_q.isoformat() if isinstance(expected_q, _dt.date) else None),
        "quarters_behind": n_behind,
        "filing_lag_days": filing_lag_days,
        "max_quarters_behind": max_quarters_behind,
    }
    if n_behind is None:
        return True, "no fundamentals panel; skip", details
    if n_behind >= max_quarters_behind:
        return (False,
                f"fundamentals {n_behind} quarter(s) behind the filing calendar: "
                f"panel at {panel_q.isoformat()} but latest-expected-filed quarter "
                f"is {expected_q.isoformat()} (10-Q lag {filing_lag_days}d). This is "
                f"a stuck/broken refresh, not the normal mid-quarter filing gap — "
                f"blocking new buys",
                details)
    return (True,
            f"fundamentals fresh: panel at {panel_q.isoformat()} == "
            f"latest-expected-filed quarter (10-Q lag {filing_lag_days}d); the "
            f"next quarter is not yet expected to be filed",
            details)


class FundamentalsFreshnessTask(PreflightTask):
    """P-FUND-FRESHNESS: fundamentals panel at the latest-expected-filed quarter.

    Buy-only gate: a stale-fundamentals finding blocks new buys but is exempt in
    sell-only runs (no buys to block), routed through ``_soft_for_sell_only``.
    """

    check_name = "P-FUND-FRESHNESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        cfg = (((ctx.config or {}).get("preflight", {}) or {})
               .get("fundamentals_freshness", {}) or {})
        if not cfg.get("enabled", True):
            return PreflightCheck(self.check_name, "soft", True, "disabled; skip")
        filing_lag = int(cfg.get("filing_lag_days", _DEFAULT_FILING_LAG_DAYS))
        max_behind = int(cfg.get("max_quarters_behind", _DEFAULT_MAX_QUARTERS_BEHIND))
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
            panel_max, _dt.date.today(), filing_lag, max_behind)
        details["path"] = str(path)
        if ok:
            return PreflightCheck(self.check_name, "hard", True, msg, details)
        # Stale: HARD buy-block in full/buy mode; soft pass in sell-only mode
        # (sell-only places no buys, so a buy-side staleness gate must not abort
        # the risk-exit path). Mirrors every other buy-only preflight gate.
        return _soft_for_sell_only(
            self.check_name, msg, run_mode=ctx.run_mode, details=details)
