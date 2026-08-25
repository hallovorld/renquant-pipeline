"""#289 — a blocked buy-leg must not burn the holding's rotation slot.

Measured 2026-08-17 live (renquant_104, logs/daily_104/2026-08-17.log): the
greedy pair-finder assigned held CRWD (ER −0.1417) to top-ranked PANW
(rank 2.812, ER −0.0589). The long-signal guard then blocked PANW at emit
time with `nonpositive_expected_return_no_long` and did a bare `continue` —
never releasing CRWD back into the pool. With max_rotations_per_bar=1, the
bar's entire rotation budget was spent on a trade that could never execute,
while GOOG (rank 2.052, ER +0.0252, net_adv +0.1669 vs the SAME holding)
sat unused. Final: 0 rotations.

The fix: BuildPairsTask pre-filters candidates through the SAME
`long_signal_ok_for_object` guard BEFORE any pair-finder runs, so an
untradeable candidate never claims a holding. Observability parity: each
pre-filtered candidate is recorded in ctx.rotations_blocked (sell=None,
stage="prefilter"), the `rotation_<reason>` counter family, and
ctx._blocked_by_ticker — the same surfaces the emit-time guard writes.
The emit-time guard stays as a normally-unreachable backstop (covered by
tests/test_signal_direction_gate.py::test_rotation_buy_leg_uses_signal_direction_gate).
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.pipeline.task_rotation import (
    BuildPairsTask,
    EmitRotationsTask,
    ValidatePairsTask,
)

BLOCK_ER = "nonpositive_expected_return_no_long"


def _cand(ticker, *, rank_score, expected_return, panel_score=None):
    # Live 2026-08-17 shape: positive panel_score (the raw gate passes),
    # sign of expected_return decides admission.
    if panel_score is None:
        panel_score = rank_score
    return CandidateResult(
        ticker=ticker, raw_score=panel_score, rank_score=rank_score,
        rs_score=0.0, detail="", expected_return=expected_return,
        expected_return_horizon_days=20, panel_score=panel_score,
        mu=expected_return, mu_horizon_days=20, sigma=0.2,
    )


def _cfg(**rotation_overrides):
    rotation = {
        "enabled": True,
        "min_expected_advantage_pct": 0.06,   # live pinned threshold
        "target_horizon_days": 20,
        "transaction_cost_pct": 0.0,
        "min_rotation_hold_days": 30,
        "lt_protection_days": 30,
        "max_rotations_per_bar": 1,           # live pinned cap
    }
    rotation.update(rotation_overrides)
    return {
        "rotation": rotation,
        "ranking": {
            "panel_scoring": {"enabled": True, "sizing": {}, "sigma_sizing": {}},
            "kelly_sizing": {"enabled": False},
        },
        "regime_params": {"BULL_CALM": {"max_position_pct": 0.15,
                                        "cash_reserve_pct": 0.0}},
        "regime": {},
        "tax": {"short_term_rate": 0.50, "long_term_rate": 0.32,
                "long_term_threshold_days": 365},
        "wash_sale_days": 0,
        "max_positions_per_sector": 0,
        "sector_map": {},
    }


def _ctx(ranked, *, config=None):
    prices = {c.ticker: 100.0 for c in ranked}
    prices["CRWD"] = 450.0   # entry 500 → sitting on a loss: no tax drag,
    #                          no LT protection — pairing math is pure ER.
    return InferenceContext(
        config=config or _cfg(),
        today=dt.date(2026, 8, 17),
        regime="BULL_CALM",
        confidence=0.69,
        portfolio_value=11_037.89,
        cash=10_000.0,
        holdings={"CRWD": SimpleNamespace(
            shares=2.0,
            rank_score=1.0,
            expected_return=-0.1417,           # measured held ER
            entry_price=500.0,
            entry_date=dt.date(2026, 5, 1),    # 108d held ≥ min_hold 30
        )},
        prices=prices,
        ranked=ranked,
    )


def _measured_20260817_candidates():
    """The issue's measured table, in panel-rank order."""
    return [
        _cand("PANW", rank_score=2.812, expected_return=-0.0589),
        _cand("CVS",  rank_score=2.325, expected_return=-0.0120),
        _cand("WELL", rank_score=2.170, expected_return=-0.1159),
        _cand("ROST", rank_score=2.060, expected_return=-0.0117),
        _cand("GOOG", rank_score=2.052, expected_return=+0.0252),
    ]


# ── the 08-17 replay ───────────────────────────────────────────────────────

def test_replay_20260817_rotates_into_goog_instead_of_burning_the_slot():
    """Pre-fix: pair=(CRWD→PANW), blocked at emit, 0 rotations.
    Post-fix: PANW is pre-filtered, pair=(CRWD→GOOG) forms and emits."""
    ctx = _ctx(_measured_20260817_candidates())

    BuildPairsTask().run(ctx)

    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] == \
        [("CRWD", "GOOG")]
    # net_adv = 0.0252 − (−0.1417) − 0 tax − 0 cost = +0.1669 (issue table)
    assert abs(ctx.rotations[0].net_advantage - 0.1669) < 1e-9

    # PANW's block is recorded with the same reason the emit guard used live.
    panw_rows = [r for r in ctx.rotations_blocked if r["buy"] == "PANW"]
    assert panw_rows == [{"sell": None, "buy": "PANW",
                          "reason": BLOCK_ER, "stage": "prefilter"}]
    assert ctx._blocked_by_ticker["PANW"] == BLOCK_ER
    # Same counter family as the emit-time guard: rotation_<reason>.
    # 4 of 5 measured candidates had nonpositive ER (PANW, CVS, WELL, ROST).
    assert ctx.counters[f"rotation_{BLOCK_ER}"] == 4

    ValidatePairsTask().run(ctx)
    EmitRotationsTask().run(ctx)

    assert [o["ticker"] for o in ctx.orders] == ["GOOG"]
    assert ctx.orders[0]["order_type"] == "ROTATION"
    exit_tickers = [t for t, _ in ctx.exits]
    assert exit_tickers == ["CRWD"]
    assert ctx.exits[0][1].exit_type == "rotation"
    assert ctx.counters.get("rotations", 0) == 1


# ── capacity is not consumed by pre-filtered candidates ────────────────────

def test_prefiltered_candidates_do_not_consume_rotation_capacity():
    """N blocked candidates ranked ABOVE the passer, cap=1 — the slot must
    still be available to the passing candidate."""
    ctx = _ctx([
        _cand("BAD1", rank_score=3.0, expected_return=-0.05),
        _cand("BAD2", rank_score=2.9, expected_return=-0.02),
        _cand("GOOD", rank_score=2.0, expected_return=+0.03),
    ])

    BuildPairsTask().run(ctx)

    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] == \
        [("CRWD", "GOOD")]
    assert [r["buy"] for r in ctx.rotations_blocked] == ["BAD1", "BAD2"]
    assert all(r["stage"] == "prefilter" and r["sell"] is None
               for r in ctx.rotations_blocked)
    assert ctx.counters[f"rotation_{BLOCK_ER}"] == 2


# ── every candidate fails ──────────────────────────────────────────────────

def test_all_candidates_fail_records_all_blocks_and_emits_nothing():
    ctx = _ctx([
        _cand("AAA", rank_score=3.0, expected_return=-0.05),
        _cand("BBB", rank_score=2.5, expected_return=-0.10),
        _cand("CCC", rank_score=2.0, expected_return=-0.01),
    ])

    BuildPairsTask().run(ctx)
    ValidatePairsTask().run(ctx)
    EmitRotationsTask().run(ctx)   # must not crash on an empty pair list

    assert ctx.rotations == []
    assert ctx.orders == []
    assert ctx.exits == []
    assert sorted(r["buy"] for r in ctx.rotations_blocked) == \
        ["AAA", "BBB", "CCC"]
    assert all(r["reason"] == BLOCK_ER and r["stage"] == "prefilter"
               for r in ctx.rotations_blocked)
    for t in ("AAA", "BBB", "CCC"):
        assert ctx._blocked_by_ticker[t] == BLOCK_ER
    assert ctx.counters[f"rotation_{BLOCK_ER}"] == 3
    # Funnel summary parity (pp_inference reads these surfaces).
    assert len(ctx.rotations_blocked) == 3
    assert ctx.counters.get("rotations", 0) == 0


# ── thesis mode inherits the pre-filter ────────────────────────────────────

def test_prefiltered_candidate_never_reaches_thesis_primary_finder(monkeypatch):
    """All three finders consume the same filtered list; assert the
    thesis_primary call site never sees a blocked candidate."""
    import renquant_pipeline.kernel.rotation as rotation_mod

    seen: list[list[str]] = []

    def _recording_finder(*args, **kwargs):
        candidates = kwargs.get("candidates")
        if candidates is None:      # defensive: positional call
            candidates = args[3]
        seen.append([c.ticker for c in candidates])
        return []

    monkeypatch.setattr(
        rotation_mod, "find_thesis_primary_pairs", _recording_finder,
    )

    ctx = _ctx(
        [
            _cand("BAD", rank_score=3.0, expected_return=-0.05),
            _cand("GOOD", rank_score=2.0, expected_return=+0.03),
        ],
        config=_cfg(mode="thesis_primary"),
    )

    BuildPairsTask().run(ctx)

    assert seen == [["GOOD"]]      # BAD filtered out before the finder ran
    assert [r["buy"] for r in ctx.rotations_blocked] == ["BAD"]
    assert ctx.rotations_blocked[0]["stage"] == "prefilter"
    assert ctx._blocked_by_ticker["BAD"] == BLOCK_ER


# ═══ 2026-08-25: the LLY→CRWD incident — guard prefilter (wash/sector/corr) ═══
# Measured live: rotation chose CRWD (ER +0.0995) with sell leg LLY; the
# correlation guard rejected the pair at VALIDATION (corr(CRWD,PANW)=0.845 ≥
# 0.70) and the engine gave up for the day — no next candidate, no other
# sell leg was tried. These tests pin the fix: guard admissibility now runs
# INSIDE the pairing walk, pair-level (holdings-minus-sell — the validator's
# exact first-pair semantics), so a blocked buy leg advances to the next
# candidate instead of ending rotation.

def _ctx_two_held(ranked, *, config=None, corr=None):
    cfg = config or _cfg()
    ctx = InferenceContext(
        config=cfg,
        today=dt.date(2026, 8, 25),
        regime="BULL_CALM",
        confidence=0.63,
        portfolio_value=10_825.0,
        cash=1_000.0,
        holdings={
            "LLY": SimpleNamespace(
                shares=1.0, rank_score=1.0, expected_return=-0.05,
                entry_price=800.0, entry_date=dt.date(2026, 5, 1)),
            "PANW": SimpleNamespace(
                shares=2.0, rank_score=1.5, expected_return=+0.02,
                entry_price=180.0, entry_date=dt.date(2026, 5, 1)),
        },
        prices={**{c.ticker: 100.0 for c in ranked},
                "LLY": 700.0, "PANW": 170.0},
        ranked=ranked,
    )
    ctx.corr_matrix = corr or {}
    return ctx


def test_replay_20260825_corr_blocked_candidate_yields_to_the_next():
    """CRWD (top-ranked) is 0.845-correlated with held PANW; NET (clean)
    ranks second. Pre-fix: pair=(LLY→CRWD) died at validation, day over.
    Post-fix: CRWD exhausts admissibility, NET pairs and validates."""
    ranked = [
        _cand("CRWD", rank_score=2.812, expected_return=+0.0995),
        _cand("NET",  rank_score=2.500, expected_return=+0.0900),
    ]
    corr = {"CRWD": {"PANW": 0.845, "LLY": -0.09},
            "NET":  {"PANW": 0.30,  "LLY": 0.10}}
    ctx = _ctx_two_held(ranked, corr=corr)
    BuildPairsTask().run(ctx)
    # The fix does better than "skip to NET": CRWD's conflict is WITH PANW,
    # so the walk finds the sell leg that RESOLVES it — selling PANW and
    # upgrading the correlated slot in place (ER +0.02 → +0.0995). Exactly
    # the validator's virtual_held semantics, found at pairing time.
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] \
        == [("PANW", "CRWD")], ctx.rotations
    ValidatePairsTask().run(ctx)
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] \
        == [("PANW", "CRWD")], "the validator agrees with the pre-filter"


def test_candidate_correlated_only_with_the_sell_leg_still_pairs():
    """Validator semantics preserved: corr is checked against
    holdings-MINUS-the-sell-leg. A candidate 0.9-correlated ONLY with the
    name being sold must still rotate in — over-blocking here would be a
    regression the validator never had."""
    ranked = [_cand("MRK", rank_score=2.8, expected_return=+0.10)]
    corr = {"MRK": {"LLY": 0.90, "PANW": 0.10}}   # only the sell leg breaches
    ctx = _ctx_two_held(ranked, corr=corr)
    BuildPairsTask().run(ctx)
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] \
        == [("LLY", "MRK")]
    ValidatePairsTask().run(ctx)
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] \
        == [("LLY", "MRK")], "the validator agrees — no over-block"


def test_wash_sale_blocked_candidate_yields_without_consuming_the_pairing():
    ranked = [
        _cand("CRWD", rank_score=2.8, expected_return=+0.10),
        _cand("NET",  rank_score=2.5, expected_return=+0.09),
    ]
    cfg = _cfg()
    cfg["wash_sale_days"] = 30
    ctx = _ctx_two_held(ranked, config=cfg,
                        corr={"CRWD": {"LLY": 0.1, "PANW": 0.1},
                              "NET": {"LLY": 0.1, "PANW": 0.2}})
    ctx.last_sell_dates = {"CRWD": dt.date(2026, 8, 10)}   # 15d ago < 30
    ctx.last_sell_pls = {"CRWD": -50.0}                    # loss → blockable
    BuildPairsTask().run(ctx)
    assert [(p.sell_ticker, p.buy_ticker) for p in ctx.rotations] \
        == [("LLY", "NET")]
    pre = [b for b in ctx.rotations_blocked
           if b.get("stage") == "prefilter" and b["buy"] == "CRWD"]
    assert len(pre) == 1 and pre[0]["reason"] == "pre_pair_wash_sale"


def test_a_candidate_failing_only_the_threshold_is_not_recorded_blocked():
    """'No advantage' is not 'blocked' — the exhaustion recorder must not
    fire for candidates that simply cleared no ER threshold."""
    # POSITIVE ER (passes the #289 signal prefilter) but clears no ER
    # threshold vs either held (raw_adv 0.055 / -0.015 < 0.06)
    ranked = [_cand("NET", rank_score=2.5, expected_return=+0.005)]
    ctx = _ctx_two_held(ranked, corr={"NET": {"LLY": 0.1, "PANW": 0.2}})
    BuildPairsTask().run(ctx)
    assert ctx.rotations == []
    assert not [b for b in getattr(ctx, "rotations_blocked", [])
                if b.get("stage") == "prefilter" and b["buy"] == "NET"]
