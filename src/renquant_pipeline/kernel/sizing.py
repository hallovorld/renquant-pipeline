"""Position sizing — confidence-scaled with oversize fallback.

Self-contained: no common/ imports.
"""
from __future__ import annotations


def _finite_float(value) -> float | None:
    import math
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def sigma_multiplier(
    sigma: float | None,
    sigma_median: float | None,
    sigma_cfg: dict | None,
) -> float:
    """Scale factor ∈ [floor, ceiling] based on predictive σ.

    High-σ candidates get smaller sizes: `mult = clip(σ_median / σ, floor, ceiling)`.
    A candidate at the universe median gets multiplier 1.0.

    Returns 1.0 when σ-sizing is disabled, σ is missing, or the median is
    not a positive finite number (i.e. no change from existing behaviour).

    sigma_cfg keys (all optional):
      enabled : bool, default False
      floor   : minimum multiplier, default 0.3
      ceiling : maximum multiplier, default 1.0  (don't oversize low-σ candidates)
    """
    if not sigma_cfg or not sigma_cfg.get("enabled", False):
        return 1.0
    if sigma is None or sigma_median is None:
        return 1.0
    try:
        s = float(sigma)
        med = float(sigma_median)
    except (TypeError, ValueError):
        return 1.0
    if not (s > 0.0 and med > 0.0):
        return 1.0
    try:
        floor = float(sigma_cfg.get("floor", 0.3))
        ceil  = float(sigma_cfg.get("ceiling", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if ceil < floor:
        return 1.0
    m = med / s
    return max(floor, min(ceil, m))


def universe_sigma_median(sigmas: list[float | None]) -> float | None:
    """Median over non-None, positive, finite σ values. None if empty."""
    import math
    vals = [float(s) for s in sigmas
            if s is not None and math.isfinite(float(s)) and float(s) > 0.0]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n % 2 == 1:
        return vals[n // 2]
    return 0.5 * (vals[n // 2 - 1] + vals[n // 2])


def conviction_score_percentiles(
    objects: list[object] | tuple[object, ...],
    *,
    attr: str = "panel_score",
) -> dict[str, float]:
    """Return ticker -> cross-sectional percentile for finite scores.

    Percentiles are in ``(0, 1]`` with average ranks for ties. This lets
    negative-centered rankers preserve relative conviction without retuning
    raw score floors/ceilings.
    """
    scored: list[tuple[float, str]] = []
    for obj in objects or []:
        ticker = getattr(obj, "ticker", None)
        score = _finite_float(getattr(obj, attr, None))
        if ticker is not None and score is not None:
            scored.append((score, str(ticker)))
    if not scored:
        return {}
    scored.sort(key=lambda item: item[0])
    n = len(scored)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i + 1
        while j < n and scored[j][0] == scored[i][0]:
            j += 1
        avg_one_based_rank = ((i + 1) + j) / 2.0
        percentile = avg_one_based_rank / n
        for _, ticker in scored[i:j]:
            out[ticker] = percentile
        i = j
    return out


def conviction_score_for_object(
    obj: object | None,
    sizing_cfg: dict | None,
    percentile_scores: dict[str, float] | None = None,
) -> float | None:
    """Return the score input for ``conviction_multiplier``.

    ``score_mode=rank_percentile`` is an opt-in fix for negative-centered
    rankers such as PatchTST: raw ``panel_score`` values are first converted
    to the same-day cross-sectional percentile, preserving relative strength
    without assuming a model-specific raw score scale.
    """
    if obj is None:
        return None
    cfg = sizing_cfg or {}
    mode = str(cfg.get("score_mode", cfg.get("input", "panel_score"))).lower()
    if mode in {"rank_percentile", "percentile", "xs_percentile", "cross_sectional_percentile"}:
        ticker = getattr(obj, "ticker", None)
        if ticker is None:
            return None
        return (percentile_scores or {}).get(str(ticker))
    if mode in {"rank_score", "calibrated_rank_score"}:
        return getattr(obj, "rank_score", None)
    return getattr(obj, "panel_score", None)


def conviction_multiplier(panel_score: float | None, sizing_cfg: dict | None) -> float:
    """Scale factor in [min_mult, 1.0] derived from a candidate's panel score.

    Rescales (panel_score - floor) / (ceiling - floor) into [min_mult, 1.0].
    Returns 1.0 when sizing is disabled, the score is missing, or the config
    is malformed — i.e. no change from existing behaviour.

    sizing_cfg keys (all optional):
      enabled  : bool, default False
      floor    : panel_score at/below which we use min_mult
      ceiling  : panel_score at/above which we use 1.0
      min_mult : minimum multiplier, default 0.5
    """
    # Audit fix SIZ-1 (Round 2 deep audit, 2026-04-25): pre-fix, NaN
    # panel_score slipped past `panel_score is None`, made `frac = NaN`
    # via `(NaN-floor)/span`, then `frac <= 0.0` and `frac >= 1.0` both
    # evaluated False on NaN → fell through to `min_mult + NaN*(...)`
    # = NaN. Conviction multiplier returned NaN, which NaN-poisoned
    # `max_pct = base * conv * sig_m` downstream in SizeAndEmitTask.
    # Now: explicit isfinite guard returns 1.0 (safe default — same
    # treatment as None).
    import math as _math
    if not sizing_cfg or not sizing_cfg.get("enabled", False):
        return 1.0
    if panel_score is None:
        return 1.0
    try:
        ps_f = float(panel_score)
    except (TypeError, ValueError):
        return 1.0
    if not _math.isfinite(ps_f):
        return 1.0
    try:
        floor    = float(sizing_cfg.get("floor", 0.0))
        ceiling  = float(sizing_cfg.get("ceiling", 1.0))
        min_mult = float(sizing_cfg.get("min_mult", 0.5))
    except (TypeError, ValueError):
        return 1.0
    if ceiling <= floor:
        return 1.0
    span = ceiling - floor
    frac = (ps_f - floor) / span
    if frac <= 0.0:
        return min_mult
    if frac >= 1.0:
        return 1.0
    return min_mult + frac * (1.0 - min_mult)


def fractional_sizing_cfg(config: dict | None) -> tuple[bool, float]:
    """Read ``execution.fractional_shares`` → ``(enabled, min_notional)``.

    Single source of truth for the three buy-emitting tasks (selection / joint /
    rotation) so the flag is threaded identically. Defaults to whole-share mode
    (``False``, ``1.0``) when the block is absent or malformed — no behaviour
    change unless strategy-104 opts in via ``execution.fractional_shares.enabled``.

    Fail-closed type discipline (Codex review #153, blocking #4): ``enabled``
    must be an ACTUAL ``bool``. A non-bool (e.g. the YAML string ``"false"``,
    which is truthy under ``bool()``) is treated as DISABLED rather than
    silently enabling fractional execution. ``min_notional`` must be a real,
    non-bool, finite, non-negative number or it falls back to ``$1``.
    """
    import math
    exec_cfg = (config or {}).get("execution", {}) or {}
    frac_cfg = exec_cfg.get("fractional_shares", {}) or {}
    # Only a genuine bool enables — a string/int/None fails CLOSED to whole-share
    # mode so a malformed config can never silently turn fractional execution on.
    enabled = frac_cfg.get("enabled", False) is True
    raw_min = frac_cfg.get("min_notional", 1.0)
    if isinstance(raw_min, bool):  # bool is an int subclass — reject explicitly
        min_notional = 1.0
    else:
        try:
            min_notional = float(raw_min)
        except (TypeError, ValueError):
            min_notional = 1.0
    if not math.isfinite(min_notional) or min_notional < 0:
        min_notional = 1.0
    return enabled, min_notional


def compute_position_size(
    portfolio_value: float,
    available_cash: float,
    max_position_pct: float,   # from regime params (already confidence-scaled by caller)
    cash_reserve_pct: float,   # from regime params (already confidence-scaled by caller)
    price: float,
    override_pct: float | None = None,
    *,
    fractional: bool = False,
    min_notional: float = 1.0,
) -> tuple[float, float]:
    """Return (target_pct, shares) for a buy order.

    override_pct: bypass reserve calc (BEAR defensive branch).

    Whole-share mode (``fractional=False``, the default) is UNCHANGED: ``shares``
    is an ``int`` and the function returns ``(0.0, 0)`` when there is not enough
    cash for at least 1 whole share within the effective cap. This is the
    measured cash-drag bottleneck for high-priced names (AVGO/BLK/GS): a small
    Kelly target (e.g. ~$400 / ~4%) buys < 1 whole share, so the order is skipped
    entirely and the slot's cash cannot deploy.

    Fractional mode (``fractional=True``, follow-up to strategy-104 #35) returns
    ``shares`` as a FLOAT rounded to 6 dp — ``round(min(target_$, cap_$,
    investable)/price, 6)`` — with NO < 1-share skip, so a sub-1-share target
    deploys at its true notional. It still returns ``(0.0, 0.0)`` (skip) when the
    resulting notional is below ``min_notional`` (dust avoidance; default ~$1).
    Fractional sizing is bounded by the SAME cap as whole-share mode, so a small
    Kelly target stays small — this only removes the rounding-to-zero skip, it
    does NOT change which names are selected or the per-name target fraction.

    Returns:
        (actual_pct, shares). ``shares`` is ``int`` when ``fractional=False`` and
        ``float`` when ``fractional=True``; both are 0 / 0.0 on a clean skip.
    """
    # Audit fix S-1 (Round 5, 2026-04-25): pre-fix, NaN price/portfolio
    # passed `<= 0` (NaN comparisons False) but then `int(NaN)` later in
    # the function raised ValueError, crashing the whole sizing path.
    # Post-fix: explicit isfinite + non-positive guard at the top.
    import math
    zero = 0.0 if fractional else 0
    if (not math.isfinite(price) or not math.isfinite(portfolio_value)
            or price <= 0 or portfolio_value <= 0):
        return 0.0, zero
    if not math.isfinite(available_cash):
        return 0.0, zero
    # Audit fix CPS-1 (Round 2 deep audit, 2026-04-25): pre-fix, NaN
    # max_position_pct or cash_reserve_pct (e.g. caller computed
    # `max_pct * confidence` and confidence was NaN — pre-G-1 leak,
    # or from bad regime config) propagated through `target_pct =
    # min(NaN, ...)` into `int(NaN * pv / price)` which raises
    # ValueError "cannot convert float NaN to integer", crashing the
    # entire SizeAndEmitTask. Now: validate finite at entry — non-finite
    # → return (0, 0) clean fallback (skip this ticker; caller logs).
    if (not math.isfinite(max_position_pct)
            or not math.isfinite(cash_reserve_pct)):
        return 0.0, zero
    if override_pct is not None and not math.isfinite(override_pct):
        return 0.0, zero

    if override_pct is not None:
        investable = available_cash
        max_pct    = override_pct
    else:
        cash_reserve = portfolio_value * cash_reserve_pct
        investable   = max(available_cash - cash_reserve, 0.0)
        max_pct      = max_position_pct

    if max_pct <= 0:
        return 0.0, zero

    target_pct = min(max_pct, investable / portfolio_value)
    if target_pct <= 0:
        return 0.0, zero

    target_dollars = target_pct * portfolio_value

    # ── Fractional-share path (strategy-104 #35 cash-drag follow-up) ──────────
    # Deploy the capped target as a fractional quantity. NO whole-share floor and
    # NO 25%/1-share oversize fallback — those only existed to repair the
    # integer rounding that fractional shares make unnecessary. The notional is
    # still bounded by `target_dollars` (= the same per-name cap whole-share mode
    # used), so this cannot oversize a high-priced name. Dust below `min_notional`
    # is skipped to avoid sub-$1 odd-lot orders that brokers may reject.
    if fractional:
        if not math.isfinite(min_notional) or min_notional < 0:
            min_notional = 0.0
        # Bound by the cap AND by cash actually on hand (investable already nets
        # the reserve / equals available_cash on the override branch).
        spend = min(target_dollars, investable)
        if not math.isfinite(spend) or spend <= 0:
            return 0.0, 0.0
        # Truncate (floor) to 6 dp rather than round-to-nearest so the realized
        # notional NEVER rounds UP past the cap / available cash (a round() here
        # let pct exceed the cap at the ~1e-8 level). Alpaca accepts up to 9 dp
        # of fractional qty; 6 dp is a safe, auditable precision.
        shares_f = math.floor((spend / price) * 1_000_000) / 1_000_000
        notional = shares_f * price
        if shares_f <= 0 or notional < min_notional:
            return 0.0, 0.0
        actual_pct = notional / portfolio_value
        return actual_pct, shares_f

    # ── Whole-share path (default, UNCHANGED) ────────────────────────────────
    # Compute shares
    shares = int(target_dollars / price)

    if shares < 1:
        # Oversize fallback: try 25% of portfolio, then re-apply the sizing
        # cap below. The fallback must not turn a capped Kelly target into an
        # oversized high-priced-stock position.
        fallback_dollars = 0.25 * portfolio_value
        shares = int(min(fallback_dollars, investable) / price)

    if shares < 1:
        # Audit fix MIN-1-SHARE (Round 4 deep audit, 2026-04-25, user spec):
        # the size cap exists to LIMIT exposure, not to BLOCK trades. If
        # confidence-scaled cap and 25% fallback both produced 0 shares but
        # we have enough investable cash for at least one share, take the
        # one share. Pre-fix, low regime confidence (e.g. 0.0041) compounded
        # with high-priced stocks caused all buys to silently disappear,
        # which the user identified as the wrong behaviour ("给的额度不够买
        # 一股的时候就买一股嘛"). Override has a sane upper bound: only
        # fires when investable >= price (≥1 share affordable).
        if investable >= price:
            shares = 1

    if shares < 1:
        return 0.0, 0

    cap_shares = int((target_pct * portfolio_value) / price)
    if shares > cap_shares:
        shares = cap_shares
    if shares < 1:
        return 0.0, 0

    actual_pct = (shares * price) / portfolio_value
    return actual_pct, shares
