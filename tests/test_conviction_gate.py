"""ConvictionGateTask: economic-conviction (mu) floor on the calibrated surface.

2026-06-22 operator review: a near-break-even raw score gets a high rank
percentile but a calibrated expected return mu ~= 0. The rank_score percentile
floor (VetoWeakBuysTask) can't see it; an expected-return floor can. Models the
live case — NFLX raw -0.26 sits just above the XGB neutral -0.27 -> mu ~= 0,
while PANW raw +0.057 -> mu +6%.

Direction is intentionally NOT handled here (no raw>0 knob): mu_floor>0 already
implies raw>the model's own neutral, and the signal_direction contract owns the
raw-vs-mu direction test. See ConvictionGateTask docstring.
"""
from __future__ import annotations

from types import SimpleNamespace

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import ConvictionGateTask


def _c(ticker: str, mu: float) -> SimpleNamespace:
    return SimpleNamespace(ticker=ticker, expected_return=mu)


def _ctx(cands, **gate) -> SimpleNamespace:
    cfg = {"conviction_gate": {"enabled": True, **gate}}
    return SimpleNamespace(
        candidates=list(cands),
        config={"ranking": {"panel_scoring": cfg}},
        counters={},
    )


def test_disabled_is_noop() -> None:
    cands = [_c("A", 0.001), _c("B", 0.06)]
    ctx = SimpleNamespace(
        candidates=list(cands),
        config={"ranking": {"panel_scoring": {}}},
        counters={},
    )
    assert ConvictionGateTask().run(ctx) is None
    assert len(ctx.candidates) == 2  # nothing dropped when gate absent


def test_enabled_but_no_floor_is_noop() -> None:
    ctx = _ctx([_c("A", 0.001)])  # enabled but mu_floor not set
    assert ConvictionGateTask().run(ctx) is None
    assert len(ctx.candidates) == 1


def test_mu_floor_drops_near_breakeven_noise() -> None:
    # PANW +6%, CSCO +4.2% clear a 3% floor; NFLX ~1%, ZM 1.5% do not.
    cands = [_c("PANW", 0.060), _c("CSCO", 0.042), _c("NFLX", 0.0096), _c("ZM", 0.015)]
    ctx = _ctx(cands, mu_floor=0.03)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "CSCO"}
    assert ctx._blocked_by_ticker["NFLX"] == "conviction:mu_below_floor"
    assert ctx._blocked_by_ticker["ZM"] == "conviction:mu_below_floor"
    assert ctx.counters["conviction_vetoed"] == 2


def test_higher_floor_keeps_only_strongest() -> None:
    # A 5% floor drops CSCO (+4.2%) too, leaving only PANW (+6%).
    cands = [_c("PANW", 0.060), _c("CSCO", 0.042), _c("NFLX", 0.0096)]
    ctx = _ctx(cands, mu_floor=0.05)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW"}


def test_nan_mu_is_dropped() -> None:
    cands = [_c("OK", 0.05), _c("NANMU", float("nan"))]
    ctx = _ctx(cands, mu_floor=0.03)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"OK"}
    assert ctx._blocked_by_ticker["NANMU"] == "conviction:mu_nan"


def test_missing_mu_is_dropped() -> None:
    cands = [_c("OK", 0.05), SimpleNamespace(ticker="NOMU")]  # no expected_return
    ctx = _ctx(cands, mu_floor=0.03)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"OK"}
    assert ctx._blocked_by_ticker["NOMU"] == "conviction:mu_nan"


def test_empty_candidates_is_safe() -> None:
    ctx = _ctx([], mu_floor=0.03)
    assert ConvictionGateTask().run(ctx) is None


# ── demean_cross_sectional (2026-06-24 research-backed intercept removal) ────
def test_intercept_lets_breakeven_names_clear_absolute_floor() -> None:
    # The live bug: a +0.0245 calibration intercept lifts NFLX/ZM above a 0.03
    # absolute floor even though they are below the cross-sectional mean.
    cands = [_c("PANW", 0.062), _c("CSCO", 0.043), _c("NFLX", 0.0326), _c("ZM", 0.0312)]
    ctx = _ctx(cands, mu_floor=0.03)  # demean default OFF
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "CSCO", "NFLX", "ZM"}


def test_demean_gates_relative_conviction_not_the_constant() -> None:
    # Same candidates; subtract the cross-sectional mean (~0.0423) first, floor 0
    # → only the above-average names survive; NFLX/ZM (below the mean) drop.
    cands = [_c("PANW", 0.062), _c("CSCO", 0.043), _c("NFLX", 0.0326), _c("ZM", 0.0312)]
    ctx = _ctx(cands, mu_floor=0.0, demean_cross_sectional=True)
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "CSCO"}
    assert ctx.counters["conviction_vetoed"] == 2


def test_demean_default_off_matches_absolute_behavior() -> None:
    cands = [_c("PANW", 0.062), _c("NFLX", 0.0326)]
    ctx = _ctx(cands, mu_floor=0.03)  # no demean key → absolute
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "NFLX"}


def test_demean_uses_full_cross_section_not_post_veto_subset() -> None:
    # FOOTGUN regression (2026-06-24): demean's reference is the FULL pre-veto
    # universe (low unconditional mean), NOT the high-rank survivors. The gate
    # runs after VetoWeakBuys, so ctx.candidates is the high-mean subset; using
    # IT as the reference + the absolute 0.03 floor admits ZERO (sell-only). The
    # full snapshot keeps the high-conviction names and drops the intercept buys.
    survivors = [_c("MU", 0.051), _c("CRWD", 0.051), _c("CME", 0.033), _c("NFLX", 0.034)]
    full = survivors + [_c(f"L{i}", mu) for i, mu in enumerate(
        [-0.02, -0.01, 0.0, 0.003, 0.006, 0.010, 0.012, 0.015])]
    ctx = _ctx(survivors, mu_floor=0.03, demean_cross_sectional=True)
    ctx._full_candidate_snapshot = list(full)  # what VetoWeakBuysTask stores
    ConvictionGateTask().run(ctx)
    kept = {c.ticker for c in ctx.candidates}
    assert kept == {"MU", "CRWD"}                      # high-conviction survive
    assert "CME" not in kept and "NFLX" not in kept    # intercept buys dropped
    assert kept, "demean over the full cross-section must not zero out (the footgun)"


def test_demean_without_snapshot_falls_back_to_candidates() -> None:
    # backward-compat: if no snapshot (e.g. veto task skipped), use ctx.candidates
    cands = [_c("PANW", 0.062), _c("CSCO", 0.043), _c("NFLX", 0.0326), _c("ZM", 0.0312)]
    ctx = _ctx(cands, mu_floor=0.0, demean_cross_sectional=True)  # no snapshot
    ConvictionGateTask().run(ctx)
    assert {c.ticker for c in ctx.candidates} == {"PANW", "CSCO"}
