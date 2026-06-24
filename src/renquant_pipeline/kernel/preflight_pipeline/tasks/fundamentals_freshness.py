"""P-FUND-FRESHNESS: fail-closed preflight gate on stale fundamental data.

2026-06-23 incident: ``sec_fundamentals_daily.parquet`` silently went 91 days
stale (last row 2026-03-24) and fed live scoring + trading with no preflight
signal, because price/sentiment were fresh. This gate makes fundamental-panel
staleness a first-class preflight control:

  - age >= critical_days (default 45)  -> HARD fail. The daily wrapper routes a
    HARD *buy-side* gate to a sell-only fallback, so new buys are blocked while
    exits / risk controls still run (operator decision 2026-06-23).
  - age >= warn_days (default 30)       -> SOFT warn (run proceeds, alert fires).
  - otherwise                           -> pass.

Config (``preflight.fundamentals_freshness``): enabled (default True),
warn_days (30), critical_days (45). Fundamentals are quarterly + forward-filled,
so 45d ~= "the latest quarter's data should be in by now".
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from renquant_pipeline.kernel.preflight import PreflightCheck

from ..base import PreflightTask
from ..ctx import PreflightContext

_DEFAULT_WARN = 30
_DEFAULT_CRITICAL = 45


def _fund_max_date(path: Path):
    if not path.exists():
        return None
    import pandas as pd  # noqa: PLC0415

    df = pd.read_parquet(path, columns=["date"])
    s = pd.to_datetime(df["date"], errors="coerce").dropna()
    return s.max().date() if len(s) else None


def fund_age_days(path: Path, today: _dt.date) -> "int | None":
    """Calendar-day age of the fundamentals panel's latest row vs ``today``."""
    d = _fund_max_date(path)
    return None if d is None else max(0, (today - d).days)


def classify_freshness(age, warn: int, critical: int):
    """Pure severity decision → (severity, ok, message). ``age is None`` → skip."""
    if age is None:
        return "soft", True, "no fundamentals panel; skip"
    if age >= critical:
        return ("hard", False,
                f"fundamentals {age}d stale (>= critical {critical}d) — blocking new buys")
    if age >= warn:
        return "soft", False, f"fundamentals {age}d stale (>= warn {warn}d)"
    return "hard", True, f"fundamentals {age}d fresh"


class FundamentalsFreshnessTask(PreflightTask):
    """P-FUND-FRESHNESS: fundamentals panel within N days of today."""

    check_name = "P-FUND-FRESHNESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        cfg = (((ctx.config or {}).get("preflight", {}) or {})
               .get("fundamentals_freshness", {}) or {})
        if not cfg.get("enabled", True):
            return PreflightCheck(self.check_name, "soft", True, "disabled; skip")
        warn = int(cfg.get("warn_days", _DEFAULT_WARN))
        critical = int(cfg.get("critical_days", _DEFAULT_CRITICAL))
        try:
            from renquant_pipeline.kernel.panel_pipeline._data_root import (  # noqa: PLC0415
                data_root,
            )
            path = data_root() / "data" / "sec_fundamentals_daily.parquet"
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(self.check_name, "soft", True,
                                  f"data_root unavailable: {exc}; skip")
        age = fund_age_days(path, _dt.date.today())
        severity, ok, msg = classify_freshness(age, warn, critical)
        details = {"age_days": age, "warn_days": warn, "critical_days": critical,
                   "path": str(path)}
        return PreflightCheck(self.check_name, severity, ok, msg, details)
