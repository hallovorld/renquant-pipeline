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
delegates to that primitive UNCONDITIONALLY and **fails closed** with a
clear error when the installed renquant-common predates the mode
(< 0.11.0, common PR #27). There is deliberately NO local
re-implementation — a pipeline-side fallback would fork the shared
calendar, the exact hazard the canonical module exists to prevent (Codex
re-review of pipeline #183). Merge order: common #27 first, then this
repo's crypto policy.
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

    NOTE: this is the ASSET-CLASS-level check only. A blanket
    ``asset_class == "crypto"`` bypass is NOT sufficient to grant the §1091
    exemption to a specific ticker — see :func:`wash_sale_applies_for_ticker`,
    which additionally requires the ticker to be an explicitly validated
    non-security spot pair (Codex review, pipeline#183: an
    asset_class=crypto-tagged tokenized security or ambiguous instrument
    must NOT silently inherit the crypto exemption). Callers making a
    per-ticker wash-sale decision MUST use
    :func:`wash_sale_applies_for_ticker`, never this function alone.
    """
    if asset_class not in KNOWN_ASSET_CLASSES:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    return asset_class != ASSET_CLASS_CRYPTO


def resolve_validated_crypto_spot_pairs(config: Any) -> frozenset[str]:
    """The EXPLICITLY DECLARED set of validated non-security crypto spot
    pairs (Codex review, pipeline#183 P5 hardening).

    Read from the top-level strategy config's ``crypto_spot_pairs`` list —
    an operator-curated allowlist of pairs actually confirmed to be genuine
    spot crypto (IRS property, IRC §1091 N/A), never derivatives, never
    tokenized securities, never anything ambiguous. Absent/empty ⇒ EMPTY
    SET (fail closed: no ticker is validated, so the §1091 bypass applies
    to nobody, until an operator explicitly declares the universe). Entries
    are normalized through :func:`renquant_common.pair_slug.as_pair` so
    ``"BTC/USD"`` and ``"BTC-USD"`` compare equal; a malformed entry is
    dropped (never silently mis-parsed into a false match).
    """
    raw = None
    if config is not None:
        getter = getattr(config, "get", None)
        raw = getter("crypto_spot_pairs") if callable(getter) else None
    if not raw:
        return frozenset()
    from renquant_common.pair_slug import as_pair  # noqa: PLC0415
    validated: set[str] = set()
    for entry in raw:
        try:
            validated.add(as_pair(entry))
        except ValueError:
            continue
    return frozenset(validated)


def is_validated_crypto_spot_pair(
    ticker: str, validated_crypto_pairs: "frozenset[str] | None"
) -> bool:
    """True only when ``ticker`` is in the EXPLICITLY DECLARED validated-spot-
    pair set. Being tagged ``asset_class=crypto`` is NOT itself sufficient —
    an unknown, unclassified, or ambiguous ticker (e.g. a tokenized security
    mistakenly routed through the crypto asset class) is never validated by
    default; this is the fail-closed half of the P5 hardening."""
    if not validated_crypto_pairs:
        return False
    from renquant_common.pair_slug import as_pair  # noqa: PLC0415
    try:
        normalized = as_pair(ticker)
    except ValueError:
        return False
    return normalized in validated_crypto_pairs


def wash_sale_applies_for_ticker(
    asset_class: str,
    ticker: str,
    validated_crypto_pairs: "frozenset[str] | None" = None,
) -> bool:
    """P5, TICKER-SCOPED (Codex review, pipeline#183): the §1091 bypass
    requires BOTH ``asset_class == "crypto"`` AND ``ticker`` being an
    explicitly validated non-security spot pair — never a blanket
    asset-class-only bypass. An ``asset_class="crypto"`` ticker that is NOT
    in the validated set fails closed: the wash-sale rule STILL applies,
    the same safe default an equity ticker gets. This is the function every
    per-ticker wash-sale call site must use; :func:`wash_sale_applies` alone
    is asset-class-level only and insufficient for a real bypass decision.
    """
    if wash_sale_applies(asset_class):
        return True  # us_equity (or any non-crypto class): unchanged
    return not is_validated_crypto_spot_pair(ticker, validated_crypto_pairs)


def sigma_clip_bounds_for(asset_class: str) -> tuple[float, float]:
    """P7: default (floor, ceiling) for annualized realized-σ clipping."""
    if asset_class not in SIGMA_CLIP_BOUNDS:
        raise ValueError(f"unknown asset_class {asset_class!r} (fail-closed)")
    return SIGMA_CLIP_BOUNDS[asset_class]


# ── P1/P2 calendar arithmetic (always-open mode) ────────────────────────────

def _require_always_open_calendar() -> Any:
    """The canonical shared calendar module, or a fail-closed error.

    Common owns calendars (crypto RFC gap M2). NO local re-implementation
    exists here by design — a pipeline-side fallback would fork the shared
    ALWAYS_OPEN primitive, the exact hazard the canonical module exists to
    prevent (Codex re-review of pipeline #183). If the installed
    renquant-common predates the mode, crypto decisions must STOP with a
    clear error, not silently degrade onto a divergent local clock.
    """
    try:
        from renquant_common import market_calendar as _mc  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - common is a hard dep
        raise RuntimeError(
            "renquant_common.market_calendar is unavailable — the crypto "
            "asset-class policy requires the canonical shared calendar "
            "(fail-closed; renquant-common is a declared dependency of "
            "renquant-pipeline)."
        ) from exc
    if not getattr(_mc, "ALWAYS_OPEN_CALENDAR_NAME", None):
        raise RuntimeError(
            "renquant_common.market_calendar has no ALWAYS_OPEN mode — the "
            "crypto asset-class policy requires renquant-common >= 0.11.0 "
            "(common PR #27; merge order: common #27 first, then pipeline "
            "#183). Refusing to fork the shared calendar with a local "
            "fallback (fail-closed)."
        )
    return _mc


def last_completed_always_open_session(ref: Any) -> datetime.date:
    """Last COMPLETED UTC calendar-day session as of ``ref`` (P1, crypto).

    Session ``D`` spans ``[D 00:00, D+1 00:00) UTC`` and completes at
    ``D+1 00:00:00 UTC`` — so as of any instant inside UTC day ``X`` the last
    completed session is ``X - 1`` (at exactly midnight the just-ended day
    counts, mirroring the NYSE ``now >= close`` rule).

    Delegates UNCONDITIONALLY to the canonical
    ``renquant_common.market_calendar`` ALWAYS_OPEN primitive (>= 0.11.0)
    and fails closed when it is absent — see
    :func:`_require_always_open_calendar`. ``ref`` may be an aware/naive
    datetime-like or a date; naive values are interpreted as UTC (the
    always-open convention, enforced by the shared primitive).
    """
    _mc = _require_always_open_calendar()
    return _mc.last_completed_session(
        ref, calendar_name=_mc.ALWAYS_OPEN_CALENDAR_NAME
    )


__all__ = [
    "ANNUALIZATION_DAYS",
    "ASSET_CLASS_CRYPTO",
    "ASSET_CLASS_US_EQUITY",
    "KNOWN_ASSET_CLASSES",
    "SETTLEMENT_DAYS",
    "SIGMA_CLIP_BOUNDS",
    "annualization_days_for",
    "is_crypto",
    "is_validated_crypto_spot_pair",
    "last_completed_always_open_session",
    "resolve_asset_class",
    "settlement_days_for",
    "resolve_validated_crypto_spot_pairs",
    "sigma_clip_bounds_for",
    "wash_sale_applies",
    "wash_sale_applies_for_ticker",
]
