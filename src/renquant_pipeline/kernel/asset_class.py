"""Asset-class execution policy — the single pipeline-side switch (RFC P11).

Crypto trading RFC (orchestrator ``doc/design/2026-07-10-crypto-trading-rfc.md``
§2.2 gap P11 / §3.0): ONE new first-class concept, ``asset_class``, read from
the TOP LEVEL of the strategy config and threaded — never re-derived from
tickers, never guessed. Every crypto behavior divergence in this repo keys off
this module so the policy cannot fork per call-site:

* P1 — freshness clock: NYSE sessions (equity) vs UTC calendar days (crypto).
* P2 — hold/streak clocks: NYSE trading days vs calendar days.
* P3 — settlement: T+1 NYSE-session queue vs instant (T+0).
* P4 — annualization: 252 vs 365 days.
* P5 — wash sale: IRC §1091 applies to equities; crypto is PROPERTY — §1091
  does not apply, so the gate / candidate block / QP mask are bypassed.
* P7 — realized-σ clip DEFAULTS: [0.05, 1.50] equity vs [0.20, 3.00] crypto
  (config still overrides; only the defaults are asset-class-aware).

Contract: an ABSENT ``asset_class`` key means ``us_equity`` and MUST leave
every equity code path byte-identical (pinned by
``tests/test_asset_class_policy.py``). Unknown values fail closed at resolve
time — a typo must not silently trade crypto under equity policy or vice
versa.

Calendar note (P1): the canonical always-open session calendar lives in
``renquant_common.market_calendar`` (``ALWAYS_OPEN`` mode — common owns
calendars, crypto RFC gap M2). :func:`last_completed_always_open_session`
consumes it when the installed renquant-common ships the mode and otherwise
degrades to the identical local UTC-day arithmetic, keeping the self-contained
kernel modules importable without common (their long-standing constraint).
"""
from __future__ import annotations

import datetime
from typing import Any

ASSET_CLASS_US_EQUITY = "us_equity"
ASSET_CLASS_CRYPTO = "crypto"

KNOWN_ASSET_CLASSES = (ASSET_CLASS_US_EQUITY, ASSET_CLASS_CRYPTO)

#: P4 — trading days per year used for annualization / de-annualization.
ANNUALIZATION_DAYS = {
    ASSET_CLASS_US_EQUITY: 252.0,
    ASSET_CLASS_CRYPTO: 365.0,
}

#: P3 — sell-proceeds settlement lag in sessions. Crypto settles instantly.
SETTLEMENT_DAYS = {
    ASSET_CLASS_US_EQUITY: 1,
    ASSET_CLASS_CRYPTO: 0,
}

#: P7 — DEFAULT annualized realized-σ clip bounds (floor, ceiling). Equity
#: values are the long-standing production defaults; crypto per RFC §3.4
#: ([0.20, 3.00] annualized-365 — realized crypto vol of 60–150%+ must not
#: pin a 1.50 ceiling or Kelly cannot discriminate vol across names).
#: Explicit config keys always win over these defaults.
SIGMA_CLIP_BOUNDS = {
    ASSET_CLASS_US_EQUITY: (0.05, 1.50),
    ASSET_CLASS_CRYPTO: (0.20, 3.00),
}


def resolve_asset_class(config: Any) -> str:
    """Resolve the running strategy config's asset class.

    ``config`` is the top-level strategy-config mapping (or anything exposing
    ``.get``). Absent / ``None`` / empty ⇒ ``us_equity`` (byte-identical
    equity behavior — the pinned default). Unknown values raise ``ValueError``
    (fail closed: never trade under a policy the config did not name).
    """
    raw = None
    if config is not None:
        getter = getattr(config, "get", None)
        raw = getter("asset_class") if callable(getter) else None
    if raw is None or raw == "":
        return ASSET_CLASS_US_EQUITY
    value = str(raw).strip().lower()
    if value not in KNOWN_ASSET_CLASSES:
        raise ValueError(
            f"unknown asset_class {raw!r} in strategy config (fail-closed); "
            f"known: {list(KNOWN_ASSET_CLASSES)}"
        )
    return value


def is_crypto(asset_class_or_config: Any) -> bool:
    """True when the given asset class (or config carrying one) is crypto."""
    if isinstance(asset_class_or_config, str):
        value = asset_class_or_config.strip().lower()
        if value not in KNOWN_ASSET_CLASSES:
            raise ValueError(
                f"unknown asset_class {asset_class_or_config!r} (fail-closed); "
                f"known: {list(KNOWN_ASSET_CLASSES)}"
            )
        return value == ASSET_CLASS_CRYPTO
    return resolve_asset_class(asset_class_or_config) == ASSET_CLASS_CRYPTO


def annualization_days_for(asset_class: str) -> float:
    """P4: 252.0 (us_equity) or 365.0 (crypto)."""
    if asset_class not in ANNUALIZATION_DAYS:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    return ANNUALIZATION_DAYS[asset_class]


def settlement_days_for(asset_class: str, *, equity_days: int = 1) -> int:
    """P3: sell-settlement lag in sessions; crypto is always 0 (instant).

    ``equity_days`` lets equity callers keep their configured T+N (legacy
    sims still pass 2) — crypto ignores it by design.
    """
    if asset_class not in SETTLEMENT_DAYS:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    if asset_class == ASSET_CLASS_CRYPTO:
        return 0
    return int(equity_days)


def wash_sale_applies(asset_class: str) -> bool:
    """P5: IRC §1091 wash-sale applies to equities only.

    Crypto is treated as PROPERTY for federal tax purposes (IRS Notice
    2014-21); §1091 covers "shares of stock or securities" and therefore does
    NOT apply. The bypass is keyed here — never a global disable.
    """
    if asset_class not in KNOWN_ASSET_CLASSES:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    return asset_class != ASSET_CLASS_CRYPTO


def sigma_clip_bounds_for(asset_class: str) -> tuple[float, float]:
    """P7: default (floor, ceiling) for annualized realized-σ clipping."""
    if asset_class not in SIGMA_CLIP_BOUNDS:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    return SIGMA_CLIP_BOUNDS[asset_class]


# ── P1/P2 calendar arithmetic (always-open mode) ────────────────────────────

def last_completed_always_open_session(ref: Any) -> datetime.date:
    """Last COMPLETED UTC calendar-day session as of ``ref`` (P1, crypto).

    Session ``D`` spans ``[D 00:00, D+1 00:00) UTC`` and completes at
    ``D+1 00:00:00 UTC`` — so as of any instant inside UTC day ``X`` the last
    completed session is ``X - 1`` (at exactly midnight the just-ended day
    counts, mirroring the NYSE ``now >= close`` rule).

    Prefers the canonical ``renquant_common.market_calendar`` ALWAYS_OPEN
    mode when the installed common ships it (>= 0.11.0); otherwise computes
    the identical result locally so kernel modules stay importable without
    common. ``ref`` may be an aware/naive datetime-like or a date; naive
    values are interpreted as UTC (the always-open convention).
    """
    try:
        from renquant_common import market_calendar as _mc  # noqa: PLC0415
    except ImportError:
        _mc = None
    if _mc is not None and getattr(_mc, "ALWAYS_OPEN_CALENDAR_NAME", None):
        return _mc.last_completed_session(
            ref, calendar_name=_mc.ALWAYS_OPEN_CALENDAR_NAME
        )
    return _utc_date_of(ref) - datetime.timedelta(days=1)


def _utc_date_of(ref: Any) -> datetime.date:
    """UTC calendar date of ``ref`` (naive ⇒ already UTC by convention)."""
    if ref is None:
        return datetime.datetime.now(datetime.timezone.utc).date()
    tzinfo = getattr(ref, "tzinfo", None)
    astimezone = getattr(ref, "astimezone", None)
    if tzinfo is not None and callable(astimezone):
        return astimezone(datetime.timezone.utc).date()
    if isinstance(ref, datetime.datetime):
        return ref.date()
    if isinstance(ref, datetime.date):
        return ref
    date_fn = getattr(ref, "date", None)  # tz-naive pandas Timestamp
    if callable(date_fn):
        return date_fn()
    raise TypeError(f"cannot interpret {ref!r} as a date/datetime")


__all__ = [
    "ANNUALIZATION_DAYS",
    "ASSET_CLASS_CRYPTO",
    "ASSET_CLASS_US_EQUITY",
    "KNOWN_ASSET_CLASSES",
    "SETTLEMENT_DAYS",
    "SIGMA_CLIP_BOUNDS",
    "annualization_days_for",
    "is_crypto",
    "last_completed_always_open_session",
    "resolve_asset_class",
    "settlement_days_for",
    "sigma_clip_bounds_for",
    "wash_sale_applies",
]
