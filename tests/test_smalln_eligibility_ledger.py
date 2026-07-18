"""Eligibility-ledger precondition for the small-n guard (amendment #207).

Covers the amendment's acceptance criteria that are decidable in this repo:

- AC-A: replaying the recorded 2026-07-16/17 partitions → CLEAN, branch
  acts, candidate_delta == {ATI, EME, BWXT};
- AC-B: synthetic failure-residue day (score_missing > 0 at n=5) →
  suppressed, status-quo floor, LOUD tag (the sentinel-side firing is the
  orchestrator PR's test);
- AC-C (pipeline side): the schema-versioned §3 block is attached on EVERY
  session — acted, suppressed, not-small-n, deconfigured, empty-scan and
  floor-unset paths — and flows through the gate registry + decision-ledger
  formatter;
- AC-D: unknown/unclassifiable exclusion reason → NOT CLEAN, ABSENT
  expected_universe counter → NOT CLEAN, and every §2 failure surface
  (contract-failed markers, panel_score_missing, rank_score_nan, promoted
  feed staleness) maps to an enumerated suppression reason;
- AC-F: generation-starved day (expected 145 / entered 5 / zero recorded
  exclusions) → NOT CLEAN even though every within-funnel record is healthy;
- AC-G: INTEGRITY share bound both sides (wash-sale above / at the bound),
  breach strictly `>`;
- approving-review expectation (a): POLICY set-identity assertion (exact
  declared string; tagged set == watchlist − declared eligible set);
- approving-review expectation (b): config-frozen watchlist outer anchor in
  the block;
- bit-identity: with the guard keys ABSENT nothing changes for prod (floors,
  kept sets, no ERROR logs).
"""
from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.decision_ledger import format_gate_verdicts
from renquant_pipeline.kernel import smalln_eligibility as elig
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    VetoWeakBuysTask,
    _apply_fund_features,
)
from renquant_pipeline import panel_scoring as twin_mod
from renquant_pipeline.inference import InferenceContext

GUARD = {"buy_floor_min_n": 12, "buy_floor_absolute_smalln": 0.50}

# Recorded 2026-07-16 governed-override session (see
# test_vetoweakbuys_smalln_guard.py for provenance).
S_0716 = [
    ("ATI", 0.557459136834569),
    ("EME", 0.5477081929836948),
    ("BWXT", 0.5329365823764792),
    ("XLI", 0.44931989852357557),
    ("XLY", 0.448368428443952),
]
FLOOR_0716 = 0.561104062882113

S_0717 = [
    ("BWXT", 0.56368464048377),
    ("EME", 0.5588641871099314),
    ("ATI", 0.5575464063449799),
    ("XLI", 0.44931989852357557),
    ("XLY", 0.448368428443952),
]
FLOOR_0717 = 0.5765004367114172

WATCHLIST_145 = [f"W{i:03d}" for i in range(140)] + [
    "ATI", "EME", "BWXT", "XLI", "XLY",
]


def _ctx(
    scores,
    *,
    watchlist=None,
    universe=None,
    blocked=None,
    counters=None,
    panel_cfg=None,
    **markers,
):
    """Kernel-shaped ctx. ``universe``/``counters`` control the emission
    state of the generation-stage instrumentation."""
    cands = [SimpleNamespace(ticker=t, rank_score=s) for t, s in scores]
    cfg = {"buy_floor": "adaptive_mean_std", "buy_floor_min": 0.20}
    cfg.update(panel_cfg or {})
    ctx = SimpleNamespace(
        candidates=cands,
        config={
            "watchlist": list(watchlist or [t for t, _ in scores]),
            "ranking": {"panel_scoring": cfg},
        },
        counters=dict(counters or {}),
    )
    if universe is not None:
        ctx.counters.setdefault(elig.EXPECTED_UNIVERSE_COUNTER, len(universe))
        ctx._smalln_expected_universe_tickers = sorted(universe)
    if blocked is not None:
        ctx._blocked_by_ticker = dict(blocked)
    for name, value in markers.items():
        setattr(ctx, name, value)
    return ctx


def _run(ctx):
    VetoWeakBuysTask().run(ctx)
    return ctx._smalln_eligibility


# ────────────────────────────────────────────────────────────────────────────
# AC-A — recorded sessions are CLEAN; the branch acts; delta = {ATI,EME,BWXT}
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scores,recorded_floor",
    [(S_0716, FLOOR_0716), (S_0717, FLOOR_0717)],
    ids=["2026-07-16", "2026-07-17"],
)
def test_ac_a_recorded_sessions_clean_act_delta(scores, recorded_floor) -> None:
    ctx = _ctx(
        scores,
        watchlist=WATCHLIST_145,
        universe=[t for t, _ in scores],
        panel_cfg=GUARD,
    )
    block = _run(ctx)
    assert block["clean"] is True
    assert block["branch_action"] == "acted"
    assert block["original_floor"] == recorded_floor
    assert block["relaxed_floor"] == 0.50
    assert block["candidate_delta"] == ["ATI", "BWXT", "EME"]
    assert block["finite_n"] == 5
    assert block["n0"] == 12
    assert block["expected_universe"] == 5
    # approving-review expectation (b): config-frozen watchlist outer anchor
    assert block["watchlist_size"] == 145
    assert block["schema_version"] == elig.SMALLN_LEDGER_SCHEMA_VERSION
    assert sorted(c.ticker for c in ctx.candidates) == ["ATI", "BWXT", "EME"]


# ────────────────────────────────────────────────────────────────────────────
# AC-B — failure-residue day: score_missing > 0 at n=5 → suppressed, LOUD
# ────────────────────────────────────────────────────────────────────────────


def test_ac_b_score_missing_residue_suppresses(caplog) -> None:
    universe = [t for t, _ in S_0716] + ["DROP1", "DROP2", "DROP3"]
    ctx = _ctx(
        S_0716,
        universe=universe,
        blocked={f"DROP{i}": "panel_score_missing" for i in (1, 2, 3)},
        counters={"panel_score_missing": 3},
        panel_cfg=GUARD,
    )
    with caplog.at_level(logging.ERROR):
        block = _run(ctx)
    assert block["clean"] is False
    assert block["branch_action"] == (
        "suppressed:funnel_integrity:panel_score_missing=3"
    )
    assert block["suppressed_reason"].startswith("funnel_integrity")
    # status-quo floor: everything vetoed exactly as live 07-16
    assert block["original_floor"] == block["relaxed_floor"] == FLOOR_0716
    assert ctx.candidates == []
    assert any(
        elig.SUPPRESSION_TAG in rec.message and rec.levelno == logging.ERROR
        for rec in caplog.records
    )


# ────────────────────────────────────────────────────────────────────────────
# AC-C (pipeline side) — the block is attached on EVERY session shape
# ────────────────────────────────────────────────────────────────────────────


def test_ac_c_block_on_normal_n_session() -> None:
    scores = [(f"T{i:02d}", 0.52 + 0.003 * i) for i in range(20)]
    ctx = _ctx(scores, universe=[t for t, _ in scores], panel_cfg=GUARD)
    block = _run(ctx)
    assert block["branch_action"] == "not_small_n"
    assert block["clean"] is True
    assert block["candidate_delta"] == []
    assert block["original_floor"] == block["relaxed_floor"]


def test_ac_c_block_when_guard_deconfigured() -> None:
    ctx = _ctx(S_0716, universe=[t for t, _ in S_0716])
    block = _run(ctx)
    assert block["branch_action"] == "deconfigured"
    assert block["n0"] is None
    assert block["clean"] is True  # partition itself is healthy


def test_ac_c_block_on_empty_scan_and_floor_unset() -> None:
    # empty candidates
    ctx = _ctx([], universe=[])
    block = _run(ctx)
    assert block["branch_action"] == "deconfigured"
    assert block["entered_scan"] == 0
    # buy_floor unset
    ctx = _ctx(S_0716, universe=[t for t, _ in S_0716],
               panel_cfg={"buy_floor": None})
    block = _run(ctx)
    assert block["branch_action"] == "deconfigured"
    assert block["original_floor"] is None


def test_ac_c_empty_generation_starved_scan_still_records_suppression(caplog) -> None:
    """Limiting AC-F shape: guard configured, NOTHING scanned, universe
    expected 145 → the no-floor path still leaves the loud suppression."""
    ctx = _ctx([], universe=WATCHLIST_145, panel_cfg=GUARD)
    with caplog.at_level(logging.ERROR):
        block = _run(ctx)
    assert block["branch_action"].startswith("suppressed:mass_balance")
    assert any(elig.SUPPRESSION_TAG in rec.message for rec in caplog.records)


def test_ac_c_gate_registry_and_ledger_formatter_rows() -> None:
    ctx = _ctx(S_0716, universe=[t for t, _ in S_0716], panel_cfg=GUARD)
    block = _run(ctx)
    rows = ctx.gate_registry.ledger_rows(run_id="r1")
    smalln_rows = [r for r in rows if r["gate"] == "smalln_eligibility"]
    assert len(smalln_rows) == 1
    assert smalln_rows[0]["verdict"] == "allow"
    assert smalln_rows[0]["reason"] == "acted"
    assert smalln_rows[0]["inputs"]["schema_version"] == 1
    verdicts = format_gate_verdicts(ctx, {}, "r1", "2026-07-16")
    ledger = [v for v in verdicts if v["gate"] == "smalln_eligibility"]
    assert len(ledger) == 1
    assert ledger[0]["inputs"] == block
    assert ledger[0]["reason"] == "acted"


def test_ledger_formatter_absent_tolerant() -> None:
    ctx = SimpleNamespace(candidates=[], holdings={})
    verdicts = format_gate_verdicts(ctx, {}, "r1", "2026-07-16")
    assert not [v for v in verdicts if v["gate"] == "smalln_eligibility"]


# ────────────────────────────────────────────────────────────────────────────
# AC-D — fail-closed on missing records and unknown reasons; every §2
# failure surface enumerates a suppression reason
# ────────────────────────────────────────────────────────────────────────────


def test_ac_d_absent_expected_universe_counter_not_clean() -> None:
    ctx = _ctx(S_0716, panel_cfg=GUARD)  # no universe emission at all
    block = _run(ctx)
    assert block["expected_universe"] is None
    assert block["branch_action"] == (
        "suppressed:mass_balance:expected_universe_absent"
    )
    assert block["original_floor"] == block["relaxed_floor"] == FLOOR_0716


def test_ac_d_unknown_exclusion_reason_not_clean() -> None:
    universe = [t for t, _ in S_0716] + ["ZZZ"]
    ctx = _ctx(
        S_0716,
        universe=universe,
        blocked={"ZZZ": "totally_new_gate_nobody_reviewed"},
        panel_cfg=GUARD,
    )
    block = _run(ctx)
    assert block["branch_action"] == (
        "suppressed:unknown_exclusion_reason:totally_new_gate_nobody_reviewed"
    )


@pytest.mark.parametrize(
    "marker,expected_reason",
    [
        (
            {"_panel_scoring_contract_failed": True},
            "failure_marker:panel_scoring_contract_failed",
        ),
        (
            {"_calibrator_contract_failed": True},
            "failure_marker:calibrator_contract_failed",
        ),
        (
            {"_feed_staleness_flagged": {"stale_days": 40}},
            "failure_marker:feed_staleness_flagged",
        ),
    ],
    ids=["panel-contract", "calibrator-contract", "feed-staleness"],
)
def test_ac_d_failure_markers_suppress(marker, expected_reason) -> None:
    ctx = _ctx(
        S_0716, universe=[t for t, _ in S_0716], panel_cfg=GUARD, **marker
    )
    block = _run(ctx)
    assert block["branch_action"] == f"suppressed:{expected_reason}"


def test_ac_d_nonfinite_scores_suppress() -> None:
    scores = S_0716 + [("NANX", float("nan"))]
    ctx = _ctx(scores, universe=[t for t, _ in scores], panel_cfg=GUARD)
    block = _run(ctx)
    assert block["nonfinite"] == 1
    assert block["branch_action"] == (
        "suppressed:funnel_integrity:rank_score_nan=1"
    )


# ────────────────────────────────────────────────────────────────────────────
# AC-F — generation-starved day: expected 145, entered 5, zero exclusions
# ────────────────────────────────────────────────────────────────────────────


def test_ac_f_generation_starved_day_suppressed(caplog) -> None:
    ctx = _ctx(S_0716, universe=WATCHLIST_145, panel_cfg=GUARD)
    with caplog.at_level(logging.ERROR):
        block = _run(ctx)
    # every within-funnel record is healthy…
    assert block["score_missing"] == 0
    assert block["nonfinite"] == 0
    assert block["pre_floor_exclusions"] == {}
    # …and the day is still NOT CLEAN by mass balance
    assert block["branch_action"] == "suppressed:mass_balance:unaccounted=140"
    assert block["original_floor"] == block["relaxed_floor"] == FLOOR_0716
    assert ctx.candidates == []  # status-quo all-veto stands
    assert any(elig.SUPPRESSION_TAG in rec.message for rec in caplog.records)


# ────────────────────────────────────────────────────────────────────────────
# AC-G — INTEGRITY share bounds (wash-sale), breach strictly above the bound
# ────────────────────────────────────────────────────────────────────────────


def _share_bound_ctx(n_wash: int, n_survivors: int):
    survivors = [(f"S{i:02d}", 0.52 + 0.004 * i) for i in range(n_survivors)]
    wash = [f"WS{i:02d}" for i in range(n_wash)]
    vol = [f"RV{i:02d}" for i in range(20 - n_wash - n_survivors)]
    universe = [t for t, _ in survivors] + wash + vol
    blocked = {t: "wash_sale:recent_sale_within_window" for t in wash}
    blocked.update({t: "risk_gate_vol" for t in vol})
    return _ctx(survivors, universe=universe, blocked=blocked, panel_cfg=GUARD)


def test_ac_g_wash_sale_share_above_bound_suppressed() -> None:
    # 5/20 = 25% > 20% bound
    block = _run(_share_bound_ctx(n_wash=5, n_survivors=5))
    assert block["branch_action"].startswith("suppressed:share_bound:wash_sale")
    assert block["pre_floor_exclusions"] == {
        "wash_sale:recent_sale_within_window": 5,
        "risk_gate_vol": 10,
    }


def test_ac_g_wash_sale_share_at_bound_clean() -> None:
    # 4/20 = 20% == bound → within (breach is strictly >); realized-vol
    # 10/20 = 50% == its bound → also within. CLEAN, branch acts.
    block = _run(_share_bound_ctx(n_wash=4, n_survivors=6))
    assert block["clean"] is True
    assert block["branch_action"] == "acted"


def test_ac_g_config_frozen_bound_override() -> None:
    # Tighten the wash-sale bound to 10%: the 4/20=20% day now breaches.
    ctx = _share_bound_ctx(n_wash=4, n_survivors=6)
    ctx.config["ranking"]["panel_scoring"][elig.CONFIG_KEY] = {
        "integrity_share_bounds": {"wash_sale": 0.10},
    }
    block = _run(ctx)
    assert block["branch_action"].startswith("suppressed:share_bound:wash_sale")


def test_invalid_bound_override_ignored_default_stays(caplog) -> None:
    ctx = _share_bound_ctx(n_wash=4, n_survivors=6)
    ctx.config["ranking"]["panel_scoring"][elig.CONFIG_KEY] = {
        "integrity_share_bounds": {"wash_sale": 1.7},  # invalid: > 1
    }
    with caplog.at_level(logging.ERROR):
        block = _run(ctx)
    # default 0.20 stays → 20% is at bound → CLEAN; misconfig logged loudly
    assert block["clean"] is True
    assert any("integrity_share_bounds" in rec.message for rec in caplog.records)


# ────────────────────────────────────────────────────────────────────────────
# Approving-review expectation (a) — POLICY set identity
# ────────────────────────────────────────────────────────────────────────────

POLICY_REASON = "policy:governed_override_eligibility"
POLICY_WATCHLIST = ["ATI", "EME", "BWXT", "XLI", "XLY", "F01", "F02", "F03"]
POLICY_CFG = {
    **GUARD,
    elig.CONFIG_KEY: {
        "policy_reasons": {
            POLICY_REASON: {"eligible": ["ATI", "EME", "BWXT", "XLI", "XLY"]},
        },
    },
}


def _policy_ctx(tagged: dict[str, str]):
    return _ctx(
        S_0716,
        watchlist=POLICY_WATCHLIST,
        universe=[t for t, _ in S_0716],
        blocked=tagged,
        panel_cfg=POLICY_CFG,
    )


def test_policy_set_identity_holds_clean() -> None:
    block = _run(_policy_ctx({t: POLICY_REASON for t in ("F01", "F02", "F03")}))
    assert block["clean"] is True
    assert block["branch_action"] == "acted"


def test_policy_set_identity_missing_tag_not_clean() -> None:
    # F03 should be excluded by the declared narrowing but carries no tag.
    block = _run(_policy_ctx({t: POLICY_REASON for t in ("F01", "F02")}))
    assert block["branch_action"] == (
        f"suppressed:policy_set_identity:{POLICY_REASON}:missing=1,unexpected=0"
    )


def test_policy_set_identity_wrong_subset_not_clean() -> None:
    # XLI is config-DECLARED eligible yet tagged excluded — the record-full
    # misapplication of pinned config (wrong subset excluded).
    tagged = {t: POLICY_REASON for t in ("F01", "F02", "F03", "XLI")}
    block = _run(_policy_ctx(tagged))
    assert block["branch_action"] == (
        f"suppressed:policy_set_identity:{POLICY_REASON}:missing=0,unexpected=1"
    )


def test_policy_malformed_declaration_fails_closed() -> None:
    ctx = _policy_ctx({t: POLICY_REASON for t in ("F01", "F02", "F03")})
    ctx.config["ranking"]["panel_scoring"][elig.CONFIG_KEY] = {
        "policy_reasons": {POLICY_REASON: {"eligible": "not-a-list"}},
    }
    block = _run(ctx)
    assert block["branch_action"] == (
        f"suppressed:policy_set_identity:{POLICY_REASON}:malformed_declaration"
    )


# ────────────────────────────────────────────────────────────────────────────
# Bit-identity — guard keys absent: nothing changes for prod
# ────────────────────────────────────────────────────────────────────────────


def test_bit_identity_guard_absent_no_behavior_change(caplog) -> None:
    scores = S_0716
    with caplog.at_level(logging.ERROR):
        # No guard keys, no universe emission (older-pipeline shape).
        ctx = _ctx(scores)
        VetoWeakBuysTask().run(ctx)
    assert ctx._panel_buy_floor == FLOOR_0716
    assert [c.ticker for c in ctx.candidates] == []  # all-veto as live
    assert not caplog.records  # NO new ERROR logs on the prod path
    block = ctx._smalln_eligibility  # additive observability only
    assert block["branch_action"] == "deconfigured"
    assert block["clean"] is False  # counter absent — recorded, not acted on
    assert block["not_clean_reason"] == "mass_balance:expected_universe_absent"


def test_bit_identity_normal_n_guard_absent_vs_present_clean() -> None:
    scores = [(f"T{i:02d}", 0.50 + 0.004 * i) for i in range(30)]
    ctx_a = _ctx(scores)
    VetoWeakBuysTask().run(ctx_a)
    ctx_b = _ctx(scores, universe=[t for t, _ in scores], panel_cfg=GUARD)
    VetoWeakBuysTask().run(ctx_b)
    assert ctx_a._panel_buy_floor == ctx_b._panel_buy_floor
    assert (
        [c.ticker for c in ctx_a.candidates]
        == [c.ticker for c in ctx_b.candidates]
    )


# ────────────────────────────────────────────────────────────────────────────
# Feed-staleness promotion (§2 condition 4 — new machine surface)
# ────────────────────────────────────────────────────────────────────────────


def _fund_panel(max_date: str) -> pd.DataFrame:
    return pd.DataFrame(
        {"date": [max_date], "ticker": ["AAA"], "roe": [0.1]}
    )


def test_feed_staleness_warning_promoted_to_marker() -> None:
    ctx = SimpleNamespace()
    rows = {"AAA": {}}
    _apply_fund_features(
        rows, _fund_panel("2026-05-01"), pd.Timestamp("2026-07-16"),
        ["AAA"], ["roe"], ctx=ctx,
    )
    marker = ctx._feed_staleness_flagged
    assert marker["stale_days"] == 76
    assert marker["max_date"] == "2026-05-01"
    assert marker["as_of"] == "2026-07-16"


def test_feed_fresh_no_marker() -> None:
    ctx = SimpleNamespace()
    _apply_fund_features(
        {"AAA": {}}, _fund_panel("2026-07-15"), pd.Timestamp("2026-07-16"),
        ["AAA"], ["roe"], ctx=ctx,
    )
    assert not hasattr(ctx, "_feed_staleness_flagged")


def test_feed_staleness_default_ctx_none_back_compat() -> None:
    _apply_fund_features(
        {"AAA": {}}, _fund_panel("2026-05-01"), pd.Timestamp("2026-07-16"),
        ["AAA"], ["roe"],
    )  # must not raise without ctx


# ────────────────────────────────────────────────────────────────────────────
# Generation-stage emission helper
# ────────────────────────────────────────────────────────────────────────────


def test_emit_expected_universe_counter_and_tickers() -> None:
    ctx = SimpleNamespace(counters={})
    elig.emit_expected_universe(ctx, ["B", "A", "A", "C"])
    assert ctx.counters[elig.EXPECTED_UNIVERSE_COUNTER] == 3
    assert ctx._smalln_expected_universe_tickers == ["A", "B", "C"]


def test_emit_expected_universe_empty() -> None:
    ctx = SimpleNamespace(counters={})
    elig.emit_expected_universe(ctx, [])
    assert ctx.counters[elig.EXPECTED_UNIVERSE_COUNTER] == 0


# ────────────────────────────────────────────────────────────────────────────
# Twin (panel_scoring) — same CLEAN gating, fail-closed on absent counter
# ────────────────────────────────────────────────────────────────────────────


def _twin_ctx(with_counter: bool):
    pairs = S_0716
    ctx = InferenceContext(
        strategy_config={
            "watchlist": [t for t, _ in pairs],
            "ranking": {"panel_scoring": {
                "enabled": True,
                "buy_floor": "adaptive_mean_std",
                "buy_floor_min": 0.20,
                **GUARD,
            }},
        },
        data_manifest={},
        artifact_manifest={"kind": "panel_ltr_xgboost"},
        market_snapshot={},
    )
    setattr(ctx, "panel_scores", {t: s for t, s in pairs})
    if with_counter:
        tickers = sorted(t for t, _ in pairs)
        setattr(ctx, "counters", {elig.EXPECTED_UNIVERSE_COUNTER: len(tickers)})
        setattr(ctx, "_smalln_expected_universe_tickers", tickers)
    return ctx


def test_twin_absent_counter_suppresses_status_quo(caplog) -> None:
    ctx = _twin_ctx(with_counter=False)
    with caplog.at_level(logging.ERROR):
        twin_mod.VetoWeakBuysTask().run(ctx)
    assert ctx._panel_buy_floor == FLOOR_0716  # status quo, not 0.50
    assert ctx.accepted_candidates == []
    assert any(elig.SUPPRESSION_TAG in rec.message for rec in caplog.records)
    block = ctx._smalln_eligibility
    assert block["branch_action"] == (
        "suppressed:mass_balance:expected_universe_absent"
    )


def test_twin_clean_partition_acts_with_delta() -> None:
    ctx = _twin_ctx(with_counter=True)
    twin_mod.VetoWeakBuysTask().run(ctx)
    assert ctx._panel_buy_floor == 0.50
    assert sorted(r["ticker"] for r in ctx.accepted_candidates) == [
        "ATI", "BWXT", "EME",
    ]
    block = ctx._smalln_eligibility
    assert block["branch_action"] == "acted"
    assert block["candidate_delta"] == ["ATI", "BWXT", "EME"]


# ────────────────────────────────────────────────────────────────────────────
# Suppression-reason precedence: first failing §2 class wins
# ────────────────────────────────────────────────────────────────────────────


def test_first_failing_class_reported() -> None:
    # Mass balance (condition 1) fails AND score_missing (condition 2) > 0:
    # the recorded reason must be the condition-1 class.
    ctx = _ctx(
        S_0716,
        universe=WATCHLIST_145,
        counters={"panel_score_missing": 2},
        panel_cfg=GUARD,
    )
    block = _run(ctx)
    assert block["branch_action"].startswith("suppressed:mass_balance")
