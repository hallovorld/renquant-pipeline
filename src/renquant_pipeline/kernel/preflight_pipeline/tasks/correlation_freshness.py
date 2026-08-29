"""P-CORR-FRESHNESS — ALARM when the served correlation artifact is stale.

Incident (orch#1065, measured 2026-08-28): the served correlation artifact
(``artifacts/prod/watchlist-correlation.json``, read via config
``regime.correlation_artifact``) carried ``as_of_date=2026-05-22`` — 95
calendar days stale — while a freshly regenerated artifact sat one directory
up (the training writer emits ``artifacts/watchlist-correlation.json``).
The correlation guard kept running on the stale matrix; measured cost at the
0.70 guard threshold: 80 dead blocks + 108 invisible conflicts. Nothing
alarmed, because P-CORR-METADATA only proves the artifact is STAMPED (leakage
contract) and deliberately does not judge how old the stamp is.

This rail is the missing alarm. It is NOT the path fix — which served path
the config should point at is a strategy-config decision that stays with
orch#1065.

Contract (frozen 2026-08-28):
  - Path: resolved exactly as the correlation guard resolves it
    (``ComputeFullSigmaTask._load_corr_from_artifact``: config key
    ``regime.correlation_artifact``, default ``prod/watchlist-correlation.json``,
    absolute passthrough, else ``<strategy_dir>/artifacts/<rel>``) via the
    shared ``_correlation_artifact_path`` helper.
  - Missing file → defer to P-CORR-METADATA (soft ok=True with a message);
    this rail never duplicates the existence failure.
  - Age = number of NYSE sessions strictly after ``as_of_date`` up to and
    including today, via ``renquant_common.market_calendar.sessions_between``.
    When the calendar primitive (or its ``pandas_market_calendars`` backend)
    is unavailable the age is counted in WEEKDAYS and the message says so.
  - Bound = config ``regime.correlation_artifact_max_age_sessions`` (default
    30). Malformed / negative / boolean values fall back to the default and
    the message says so.
  - Severity is ALWAYS soft — this is an alarm; the guard still functions on
    stale data and the daily run must never be blocked by this rail.
      * age > bound  → ok=False, message names as_of_date, age, bound and the
        fix pointer.
      * age <= bound → ok=True, message names the age.
      * as_of_date absent / unparseable → ok=False "freshness UNVERIFIED".
        Absence must NOT read as fresh (invented-keys-return-silent-empties).
        Parsing is STRICT: exactly ``YYYY-MM-DD`` or a fully parseable ISO
        datetime (trailing ``Z`` tolerated) — no prefix slicing.
      * as_of_date AFTER today → ok=False "freshness UNVERIFIED" naming both
        dates; a future stamp is a broken stamp, not a fresh one.
        ``as_of_date == today`` is age 0 / ok.
        ``data_window_end`` is accepted as the stamp when ``as_of_date`` is
        absent (the v2 writer sets both to the same value); the field used is
        reported in ``details["stamp_field"]``.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

from renquant_pipeline.kernel.preflight import (  # noqa: PLC0415 (legacy bridge)
    PreflightCheck,
    _correlation_artifact_path,
)

from ..base import PreflightTask
from ..ctx import PreflightContext

#: Default freshness bound in NYSE sessions. ~6 trading weeks: the training
#: writer regenerates the artifact on every retrain (rolling 120-bar window),
#: so a stamp older than this means the served copy is not being refreshed.
DEFAULT_MAX_AGE_SESSIONS = 30

_CONFIG_KEY = "correlation_artifact_max_age_sessions"
#: A stamp must START with a strict ``YYYY-MM-DD`` (the calendar-validity
#: check is left to ``fromisoformat``); the remainder must be an ISO time
#: suffix or nothing. Rejects the basic form ``20260828`` even on Python
#: 3.11+, where ``date.fromisoformat`` would otherwise accept it.
_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_FIX_POINTER = (
    "regenerate to the served path or point the config at the maintained "
    "file; see orch#1065"
)


def _today() -> _dt.date:
    """Module-level clock so tests can freeze 'today' without touching
    ``datetime.date`` globally."""
    return _dt.date.today()


def _parse_stamp(value: Any) -> _dt.date | None:
    """Coerce an artifact date stamp to ``datetime.date``; ``None`` on failure.

    STRICT (PR #299 review): the WHOLE string must be either an ISO date
    (exactly ``YYYY-MM-DD``) or a fully parseable ISO datetime
    (``datetime.fromisoformat`` on the entire string, tolerating a trailing
    ``Z``). No prefix slicing — ``2026-08-28garbage``, ``2026-08-28T``,
    ``2026-13-01`` and ``20260828`` are all parse failures, which the caller
    reports as UNVERIFIED. Numbers, empty strings and other types fail too.
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _ISO_PREFIX.match(text):
        return None
    if len(text) == 10:
        try:
            return _dt.date.fromisoformat(text)
        except ValueError:
            return None
    if text[10] not in ("T", " "):
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _resolve_bound(config: dict) -> tuple[int, str | None]:
    """Return (bound, note). ``note`` is set when the configured value was
    malformed and the default was used instead."""
    regime_cfg = config.get("regime", {}) or {}
    if _CONFIG_KEY not in regime_cfg:
        return DEFAULT_MAX_AGE_SESSIONS, None
    raw = regime_cfg.get(_CONFIG_KEY)
    bound: int | None = None
    if not isinstance(raw, bool):
        try:
            candidate = int(raw)
            if isinstance(raw, float) and candidate != raw:
                candidate = None
            bound = candidate
        except (TypeError, ValueError):
            bound = None
    if bound is None or bound < 0:
        return (
            DEFAULT_MAX_AGE_SESSIONS,
            f"config regime.{_CONFIG_KEY}={raw!r} is malformed (expected a "
            f"non-negative integer); using default {DEFAULT_MAX_AGE_SESSIONS}",
        )
    return bound, None


def _age_in_sessions(as_of: _dt.date, today: _dt.date) -> tuple[int, str, str | None]:
    """Return (age, calendar_label, note).

    ``age`` counts sessions strictly after ``as_of`` up to and including
    ``today`` (an artifact stamped today is 0 sessions old). A FUTURE stamp
    is not an age at all — the caller must reject it before calling here
    (PR #299 review: it used to collapse to 0 and pass as fresh); this
    function raises ``ValueError`` on one as a guard. ``calendar_label`` is
    ``"NYSE"`` when the shared calendar primitive answered,
    ``"weekday-fallback"`` otherwise, with ``note`` explaining why.
    """
    if as_of > today:
        raise ValueError(
            f"as_of={as_of.isoformat()} is after today={today.isoformat()}"
        )
    if as_of == today:
        return 0, "NYSE", None
    start = as_of + _dt.timedelta(days=1)
    try:
        from renquant_common.market_calendar import sessions_between  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — degrade, never block
        return (
            _weekdays_between(start, today),
            "weekday-fallback",
            f"renquant_common.market_calendar unavailable ({exc}); age counted "
            "in WEEKDAYS, not NYSE sessions",
        )
    try:
        sessions = sessions_between(start, today, calendar_name="NYSE")
        return int(len(sessions)), "NYSE", None
    except Exception as exc:  # noqa: BLE001 — CalendarUnavailableError etc.
        return (
            _weekdays_between(start, today),
            "weekday-fallback",
            f"NYSE calendar backend unavailable ({exc}); age counted in "
            "WEEKDAYS, not NYSE sessions",
        )


def _weekdays_between(start: _dt.date, end: _dt.date) -> int:
    """Count Mon–Fri days in ``[start, end]`` (both inclusive)."""
    if start > end:
        return 0
    n = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            n += 1
        day += _dt.timedelta(days=1)
    return n


class CorrelationFreshnessTask(PreflightTask):
    """P-CORR-FRESHNESS — soft alarm on a stale served correlation artifact.

    Sibling of P-CORR-METADATA (which owns existence / stamp-validity for the
    leakage contract). See the module docstring for the frozen contract.
    """

    check_name = "P-CORR-FRESHNESS"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        p: Path = _correlation_artifact_path(ctx.config, ctx.strategy_dir)
        bound, bound_note = _resolve_bound(ctx.config)
        details: dict[str, Any] = {
            "path": str(p),
            "max_age_sessions": bound,
        }
        if bound_note:
            details["bound_note"] = bound_note

        if not p.exists():
            return PreflightCheck(
                self.check_name, "soft", True,
                f"correlation artifact missing at {p}; existence is "
                "P-CORR-METADATA's contract — freshness check deferred",
                details=details,
            )

        try:
            raw = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            details["error"] = str(exc)
            return PreflightCheck(
                self.check_name, "soft", False,
                f"correlation artifact freshness UNVERIFIED: unreadable at "
                f"{p} ({exc}); a stamp that cannot be read must not pass as "
                "fresh",
                details=details,
            )

        stamp_field, stamp_raw = self._pick_stamp(raw)
        details["stamp_field"] = stamp_field
        details["as_of_date"] = stamp_raw
        as_of = _parse_stamp(stamp_raw)
        if as_of is None:
            what = (
                "no as_of_date / data_window_end stamp"
                if stamp_field is None
                else f"{stamp_field}={stamp_raw!r} is not a parseable date"
            )
            return PreflightCheck(
                self.check_name, "soft", False,
                f"correlation artifact freshness UNVERIFIED at {p}: {what}; "
                "absence is not freshness — regenerate with schema_version=2 "
                f"({_FIX_POINTER})",
                details=details,
            )

        today = _today()
        details["as_of_date"] = as_of.isoformat()
        details["today"] = today.isoformat()
        if as_of > today:
            # A future-dated stamp cannot be aged; it is a broken stamp, not a
            # fresh one (PR #299 review — it previously read as 0 sessions old).
            return PreflightCheck(
                self.check_name, "soft", False,
                f"correlation artifact freshness UNVERIFIED at {p}: "
                f"as_of_date={as_of.isoformat()} is in the FUTURE relative to "
                f"today={today.isoformat()}; a future stamp is not freshness — "
                f"{_FIX_POINTER}",
                details=details,
            )
        age, calendar_label, cal_note = _age_in_sessions(as_of, today)
        details.update({
            "as_of_date": as_of.isoformat(),
            "today": today.isoformat(),
            "age_sessions": age,
            "calendar": calendar_label,
        })
        if cal_note:
            details["calendar_note"] = cal_note
        unit = "NYSE sessions" if calendar_label == "NYSE" else "weekdays (calendar fallback)"
        notes = "; ".join(n for n in (bound_note, cal_note) if n)
        suffix = f" [{notes}]" if notes else ""

        if age > bound:
            return PreflightCheck(
                self.check_name, "soft", False,
                f"correlation artifact STALE: as_of_date={as_of.isoformat()} is "
                f"{age} {unit} old, above max_age_sessions={bound}, at {p}; the "
                "correlation guard is running on stale correlations — "
                f"{_FIX_POINTER}{suffix}",
                details=details,
            )
        return PreflightCheck(
            self.check_name, "soft", True,
            f"correlation artifact as_of_date={as_of.isoformat()} is {age} "
            f"{unit} old (max_age_sessions={bound}){suffix}",
            details=details,
        )

    @staticmethod
    def _pick_stamp(raw: Any) -> tuple[str | None, Any]:
        """Return (field_name, value) for the freshness stamp.

        ``as_of_date`` is authoritative; ``data_window_end`` is accepted when
        ``as_of_date`` is absent (the v2 writer stamps both identically).
        Legacy v1 flat matrices carry neither → ``(None, None)``.
        """
        if not isinstance(raw, dict):
            return None, None
        for field in ("as_of_date", "data_window_end"):
            if field in raw and raw.get(field) is not None:
                return field, raw.get(field)
        return None, None
