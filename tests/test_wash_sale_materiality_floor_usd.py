"""Governed wash-sale materiality floor — `risk.wash_sale.materiality_floor_usd`.

Governing contract: renquant-strategy-104
doc/design/2026-08-02-wash-sale-materiality-floor.md (merged 2026-08-02);
enforcement is pipeline#223. This knob is DISTINCT from the earlier
`wash_sale_min_material_npv` NPV floor (tests: test_wash_sale_materiality_floor.py):
it waives an ALREADY-BLOCKED name when its estimated foregone tax benefit
(event-net disallowed loss × assumed marginal rate, ceil'd to the cent) is
<= the configured floor, and stamps a decision-trace record.

The suite proves, per the design:
  * floor 0.0 / absent → BYTE-IDENTICAL decisions and log messages (A/B
    against a baseline expectation derived from the unchanged detection
    function, on a synthetic session fixture covering every branch);
  * the zero-floor short-circuit is normative — a CONSTRUCTED name whose
    estimate is exactly $0.00 still blocks at floor == 0.0;
  * same-event netting: a gain+loss disposal nets through the EXISTING lot
    engine before the estimate (the disposed-lot netting defect class);
  * estimate unavailable → the block STANDS, stamped `estimate_unavailable`;
  * invalid / above-ceiling config values → floor DISABLED + loud finding
    recorded (never silently clamped, never waiving anything);
  * the waive stamp shape incl. config_fingerprint;
  * per-name aggregation: mixed waive/stand sessions keep the mass-block
    invariant's aggregate semantics honest.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from types import SimpleNamespace

import pytest

from renquant_pipeline.kernel.pipeline.task_candidates import (
    WashSaleFilterTask,
    collect_wash_sale_decision_records,
)
from renquant_pipeline.kernel.selection import (
    WASH_SALE_ASSUMED_MARGINAL_RATE_DEFAULT,
    WASH_SALE_MATERIALITY_FLOOR_CEILING_USD,
    estimate_foregone_wash_sale_tax_benefit_usd,
    is_wash_sale_blocked_with_cost,
    resolve_wash_sale_materiality_policy,
    wash_sale_materiality_floor_waives,
)

TODAY = datetime.date(2026, 7, 30)

# Synthetic captured-session fixture — every branch of the gate.
# (ticker → (last_sell_date, realized P/L)); None P/L = unknown (the MU case).
SESSION = {
    "GAINX": (datetime.date(2026, 7, 25), +500.0),   # gain sale in window
    "TINY": (datetime.date(2026, 7, 10), -1.43),     # trivial loss in window
    "BIGL": (datetime.date(2026, 7, 4), -488.43),    # material loss in window
    "UNKN": (datetime.date(2026, 7, 17), None),      # P/L unknown → binary block
    "OLDL": (datetime.date(2026, 5, 1), -5000.0),    # loss OUTSIDE the window
    "NONE": (None, None),                            # no recent sale
}
LAST_SELL_DATES = {t: d for t, (d, _) in SESSION.items() if d is not None}
LAST_SELL_PLS = {t: pl for t, (_, pl) in SESSION.items() if SESSION[t][0] is not None}

BASE_CFG = {"wash_sale_days": 30}


def _floor_cfg(floor, rate=None, extra=None):
    wash: dict = {"materiality_floor_usd": floor}
    if rate is not None:
        wash["assumed_marginal_rate"] = rate
    if extra:
        wash.update(extra)
    return {**BASE_CFG, "risk": {"wash_sale": wash}}


def _tc(ticker: str, cfg: dict) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker, today=TODAY, config=cfg,
        last_sell_dates=dict(LAST_SELL_DATES),
        last_sell_pls=dict(LAST_SELL_PLS),
        blocked_by=None,
    )


def _run_session(cfg: dict):
    """Run WashSaleFilterTask over the fixture; return per-ticker outcomes."""
    outcomes = {}
    tctxs = []
    for ticker in SESSION:
        tc = _tc(ticker, cfg)
        result = WashSaleFilterTask().run(tc)
        outcomes[ticker] = {
            "result": result,
            "blocked_by": tc.blocked_by,
            "waiver": getattr(tc, "wash_sale_waiver", None),
            "findings": getattr(tc, "wash_sale_floor_findings", None),
        }
        tctxs.append(tc)
    return outcomes, tctxs


# ── 1. floor=0 byte-invariance: the A/B against a baseline expectation ───────


def _baseline_expectation():
    """Today's behavior, derived from the UNCHANGED detection function.

    The pre-floor gate is exactly: blocked → `wash_sale:{reason}` +
    `DROP_WashSaleFilter [T]: {reason}` (INFO); passed with a substantive
    reason → `PASS_WashSaleFilter [T]: {reason} (cost_npv=$X.XX)` (DEBUG).
    """
    expected = {}
    messages = []
    for ticker in SESSION:
        blocked, reason, cost_npv = is_wash_sale_blocked_with_cost(
            ticker, TODAY, LAST_SELL_DATES, LAST_SELL_PLS, 30,
            tax_rate=0.30, discount_rate=0.05, estimated_hold_years=2.0,
            expected_dollar_return=None, min_material_npv_cost=0.0,
        )
        if blocked:
            expected[ticker] = (False, f"wash_sale:{reason}")
            messages.append(
                ("INFO", f"DROP_WashSaleFilter [{ticker}]: {reason}"))
        else:
            expected[ticker] = (None, None)
            if reason and "no recent sale" not in reason \
                    and "disabled" not in reason:
                messages.append((
                    "DEBUG",
                    f"PASS_WashSaleFilter [{ticker}]: {reason} "
                    f"(cost_npv=${cost_npv:.2f})",
                ))
    return expected, messages


def _captured(caplog):
    return [
        (r.levelname, r.getMessage())
        for r in caplog.records
        if r.name == "kernel.pipeline.candidates"
    ]


@pytest.mark.parametrize(
    "cfg",
    [
        BASE_CFG,                 # key ABSENT — today's config, verbatim
        _floor_cfg(0.0),          # explicit 0.0 — must be indistinguishable
    ],
    ids=["floor-key-absent", "floor-explicit-zero"],
)
def test_floor_zero_is_byte_identical_to_baseline(cfg, caplog):
    """The A/B: code path present vs baseline expectation, byte-for-byte —
    decisions, blocked_by strings, and log messages. No waiver, no finding."""
    expected, expected_messages = _baseline_expectation()
    caplog.set_level(logging.DEBUG, logger="kernel.pipeline.candidates")
    outcomes, _ = _run_session(cfg)
    for ticker, (want_result, want_blocked_by) in expected.items():
        got = outcomes[ticker]
        assert got["result"] is want_result, ticker
        assert got["blocked_by"] == want_blocked_by, ticker
        assert got["waiver"] is None, ticker
        assert got["findings"] is None, ticker
    assert _captured(caplog) == expected_messages


def test_floor_zero_absent_and_explicit_zero_are_bytewise_equal(caplog):
    """A (absent) vs B (explicit 0.0) — identical outcome maps AND identical
    formatted log streams."""
    caplog.set_level(logging.DEBUG, logger="kernel.pipeline.candidates")
    outcomes_a, _ = _run_session(BASE_CFG)
    messages_a = _captured(caplog)
    caplog.clear()
    outcomes_b, _ = _run_session(_floor_cfg(0.0))
    messages_b = _captured(caplog)
    assert {t: (o["result"], o["blocked_by"]) for t, o in outcomes_a.items()} \
        == {t: (o["result"], o["blocked_by"]) for t, o in outcomes_b.items()}
    assert messages_a == messages_b


def test_constructed_zero_dollar_estimate_still_blocks_at_floor_zero():
    """The design's constructed case: a name whose estimated foregone tax
    benefit is exactly $0.00 must STILL block at floor == 0.0 — the
    `estimate <= floor` comparison is never evaluated there. An
    implementation applying the comparison uniformly would waive it
    (0.00 <= 0.0) — a contract violation."""
    # rate 0.0 is an explicitly configured, valid value; it makes TINY's
    # estimate exactly $0.00 — proven first, so the case is real:
    assert estimate_foregone_wash_sale_tax_benefit_usd(
        -1.43, assumed_marginal_rate=0.0) == 0.0
    cfg = _floor_cfg(0.0, rate=0.0)
    policy = resolve_wash_sale_materiality_policy(cfg)
    assert policy.findings == ()          # both values valid — no finding
    assert policy.floor_usd == 0.0
    tc = _tc("TINY", cfg)
    assert WashSaleFilterTask().run(tc) is False
    assert str(tc.blocked_by).startswith("wash_sale:loss sale")
    assert getattr(tc, "wash_sale_waiver", None) is None


# ── 2. The estimator: ceil-to-cent, unavailable, defect-class netting ────────


def test_estimate_rounds_up_to_the_cent():
    # 1.43 × 0.40 = 0.572 → $0.58 (UP), never $0.57.
    assert estimate_foregone_wash_sale_tax_benefit_usd(
        -1.43, assumed_marginal_rate=0.40) == 0.58
    # An exact cent value stays put (ceil of an exact value is itself).
    assert estimate_foregone_wash_sale_tax_benefit_usd(
        -1.00, assumed_marginal_rate=0.40) == 0.40
    # 488.43 × 0.40 = 195.372 → $195.38.
    assert estimate_foregone_wash_sale_tax_benefit_usd(
        -488.43, assumed_marginal_rate=0.40) == 195.38


def test_estimate_unavailable_inputs_return_none():
    for bad in (None, float("nan"), float("inf"), "oops", True):
        assert estimate_foregone_wash_sale_tax_benefit_usd(
            bad, assumed_marginal_rate=0.40) is None, bad


def test_same_event_netting_uses_the_existing_lot_engine():
    """A gain lot and a loss lot in ONE disposal net BEFORE the estimate.

    Defect class (fixed 2026-07-27 in compute_disposed_lot_tax): gain lots
    taxed while same-event loss lots were ignored. The estimator-side twin
    would be a losses-only 'disallowed loss'. Refuted here with numbers."""
    from renquant_pipeline.kernel.exits import DisposedTaxLot
    from renquant_pipeline.kernel.portfolio import (
        event_net_realized_pnl_from_disposed_lots,
    )
    sell_date = datetime.date(2026, 7, 20)
    lots = [
        # basis $50 → sold at $100 → +$50 gain lot
        DisposedTaxLot(shares=1.0, price=50.0, date=datetime.date(2026, 6, 1)),
        # basis $180 → sold at $100 → −$80 loss lot
        DisposedTaxLot(shares=1.0, price=180.0, date=datetime.date(2026, 6, 1)),
    ]
    net = event_net_realized_pnl_from_disposed_lots(100.0, sell_date, lots)
    assert net == pytest.approx(-30.0)
    est = estimate_foregone_wash_sale_tax_benefit_usd(
        net, assumed_marginal_rate=0.40)
    assert est == 12.00           # netted: 30 × 0.40
    assert est != 32.00           # the losses-only (defect-class) figure


def test_lot_engine_unavailability_is_none_not_zero():
    from renquant_pipeline.kernel.portfolio import (
        event_net_realized_pnl_from_disposed_lots,
    )
    sell_date = datetime.date(2026, 7, 20)
    assert event_net_realized_pnl_from_disposed_lots(
        100.0, sell_date, []) is None
    assert event_net_realized_pnl_from_disposed_lots(
        float("nan"), sell_date, []) is None
    assert event_net_realized_pnl_from_disposed_lots(
        100.0, None, []) is None


# ── 3. floor > 0: waive / stand / estimate_unavailable at the gate ───────────


def test_waive_stamp_shape_including_config_fingerprint():
    cfg = _floor_cfg(5.0)
    policy = resolve_wash_sale_materiality_policy(cfg)
    tc = _tc("TINY", cfg)
    assert WashSaleFilterTask().run(tc) is None     # proceeds
    assert tc.blocked_by is None
    waiver = tc.wash_sale_waiver
    assert waiver == {
        "gate": "wash_sale",
        "ticker": "TINY",
        "waived": True,
        "est_foregone_tax_usd": 0.58,
        "floor_usd": 5.0,
        "config_fingerprint": policy.config_fingerprint,
    }
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", waiver["config_fingerprint"])


def test_config_fingerprint_tracks_the_policy_subtree():
    fp5 = resolve_wash_sale_materiality_policy(_floor_cfg(5.0)).config_fingerprint
    fp5_again = resolve_wash_sale_materiality_policy(_floor_cfg(5.0)).config_fingerprint
    fp6 = resolve_wash_sale_materiality_policy(_floor_cfg(6.0)).config_fingerprint
    assert fp5 == fp5_again        # deterministic
    assert fp5 != fp6              # attributable to the exact policy values


def test_material_loss_stands_above_the_floor():
    tc = _tc("BIGL", _floor_cfg(5.0))
    assert WashSaleFilterTask().run(tc) is False    # 195.38 > 5.00
    assert str(tc.blocked_by).startswith("wash_sale:loss sale")
    assert "[estimate_unavailable]" not in tc.blocked_by
    assert getattr(tc, "wash_sale_waiver", None) is None


def test_estimate_unavailable_block_stands_and_is_stamped():
    tc = _tc("UNKN", _floor_cfg(5.0))
    assert WashSaleFilterTask().run(tc) is False
    assert tc.blocked_by == (
        "wash_sale:sold 13d ago (P/L unknown — binary block)"
        " [estimate_unavailable]"
    )
    assert getattr(tc, "wash_sale_waiver", None) is None


def test_boundary_estimate_equal_to_floor_waives():
    # est == floor waives (contract: `estimate <= floor`).  0.58 floor, TINY.
    tc = _tc("TINY", _floor_cfg(0.58))
    assert WashSaleFilterTask().run(tc) is None
    assert tc.wash_sale_waiver["est_foregone_tax_usd"] == 0.58
    # one cent below the estimate → stands.
    tc = _tc("TINY", _floor_cfg(0.57))
    assert WashSaleFilterTask().run(tc) is False


# ── 4. Fail-closed validation: disabled + loud finding, never a clamp ────────


@pytest.mark.parametrize(
    "floor",
    ["5.0", True, [], {}, None, -1.0, -0.01,
     float("nan"), float("inf"), 50.01, 100.0],
    ids=["quoted-number", "bool", "list", "dict", "null", "neg1", "neg001",
         "nan", "inf", "just-over-ceiling", "double-ceiling"],
)
def test_invalid_floor_value_disables_and_records_a_finding(floor):
    policy = resolve_wash_sale_materiality_policy(_floor_cfg(floor))
    assert policy.floor_usd == 0.0
    assert len(policy.findings) == 1
    records = policy.finding_records()
    assert records[0]["gate"] == "wash_sale"
    assert records[0]["record"] == "config_validation_finding"
    assert records[0]["waived"] is False
    assert records[0]["config_fingerprint"] == policy.config_fingerprint


def test_ceiling_finding_names_the_design_amendment_requirement():
    policy = resolve_wash_sale_materiality_policy(_floor_cfg(100.0))
    assert "design ceiling" in policy.findings[0]
    assert "amending the s104 design" in policy.findings[0]


@pytest.mark.parametrize(
    "rate", ["0.4", -0.1, 1.5, float("nan"), True],
    ids=["quoted", "negative", "over-ceiling", "nan", "bool"],
)
def test_invalid_rate_disables_the_floor_even_when_floor_is_valid(rate):
    """`never let a bad value waive anything`: a bad rate must not feed the
    waiver arithmetic at a substituted default — the floor goes dark."""
    policy = resolve_wash_sale_materiality_policy(_floor_cfg(5.0, rate=rate))
    assert policy.floor_usd == 0.0
    assert policy.findings
    tc = _tc("TINY", _floor_cfg(5.0, rate=rate))
    assert WashSaleFilterTask().run(tc) is False        # nothing waives
    assert getattr(tc, "wash_sale_waiver", None) is None
    assert tc.wash_sale_floor_findings                  # recorded on output


def test_valid_boundaries_are_accepted_without_findings():
    policy = resolve_wash_sale_materiality_policy(
        _floor_cfg(WASH_SALE_MATERIALITY_FLOOR_CEILING_USD, rate=1.0))
    assert policy.floor_usd == 50.0
    assert policy.assumed_marginal_rate == 1.0
    assert policy.findings == ()
    absent = resolve_wash_sale_materiality_policy(BASE_CFG)
    assert absent.floor_usd == 0.0
    assert absent.assumed_marginal_rate == WASH_SALE_ASSUMED_MARGINAL_RATE_DEFAULT
    assert absent.findings == ()


def test_invalid_floor_records_finding_but_decisions_stay_baseline(caplog):
    """With a bad configured value the floor is DISABLED — every decision and
    message equals the floor-0 baseline; the ONLY addition is the finding."""
    expected, expected_messages = _baseline_expectation()
    caplog.set_level(logging.DEBUG, logger="kernel.pipeline.candidates")
    outcomes, _ = _run_session(_floor_cfg(100.0))
    for ticker, (want_result, want_blocked_by) in expected.items():
        assert outcomes[ticker]["result"] is want_result, ticker
        assert outcomes[ticker]["blocked_by"] == want_blocked_by, ticker
        assert outcomes[ticker]["waiver"] is None, ticker
        assert outcomes[ticker]["findings"], ticker     # recorded, loudly
    assert _captured(caplog) == expected_messages


# ── 5. Per-name aggregation + the mass-block invariant ───────────────────────


def _blocked_map(outcomes) -> dict:
    return {
        t: o["blocked_by"] for t, o in outcomes.items()
        if o["blocked_by"] is not None
    }


def test_mixed_session_waives_per_name_only():
    outcomes, _ = _run_session(_floor_cfg(5.0))
    blocked = _blocked_map(outcomes)
    # TINY waived; BIGL + UNKN stand; the rest pass as before.
    assert set(blocked) == {"BIGL", "UNKN"}
    assert outcomes["TINY"]["waiver"]["waived"] is True
    assert outcomes["GAINX"]["blocked_by"] is None
    assert outcomes["OLDL"]["blocked_by"] is None


def test_mass_block_counts_standing_blocks_not_waived_names():
    from renquant_pipeline.kernel.pipeline.task_funnel_integrity import (
        WashSaleMassBlockInvariant,
        _wash_sale_count,
    )
    outcomes, _ = _run_session(_floor_cfg(5.0))
    blocked = _blocked_map(outcomes)
    # estimate_unavailable-stamped blocks remain wash_sale-family standing
    # blocks; the waived name is not counted.
    assert _wash_sale_count(blocked, {}) == 2
    view = SimpleNamespace(blocked=blocked, counters={}, history=())
    finding = WashSaleMassBlockInvariant().evaluate(view, {"min_count": 2})
    assert finding is not None
    assert finding.evidence["wash_sale_blocked"] == 2
    # Same session, min_count above the standing count → quiet.
    assert WashSaleMassBlockInvariant().evaluate(view, {"min_count": 3}) is None


def test_all_waived_session_does_not_fire_the_mass_block():
    """Documented consequence: if EVERY blocked name waives, the mass block
    does not fire — no name was actually suppressed; each waive is
    individually accounted for by its decision-trace record."""
    from renquant_pipeline.kernel.pipeline.task_funnel_integrity import (
        WashSaleMassBlockInvariant,
    )
    outcomes = {}
    cfg = _floor_cfg(5.0)
    for i in range(6):
        ticker = "TINY"
        tc = _tc(ticker, cfg)
        WashSaleFilterTask().run(tc)
        outcomes[f"T{i}"] = {
            "blocked_by": tc.blocked_by,
            "waiver": getattr(tc, "wash_sale_waiver", None),
        }
    assert all(o["blocked_by"] is None for o in outcomes.values())
    assert all(o["waiver"] for o in outcomes.values())
    view = SimpleNamespace(blocked={}, counters={}, history=())
    assert WashSaleMassBlockInvariant().evaluate(view, {"min_count": 2}) is None


# ── 6. The record surface the run bundle collects ────────────────────────────


def test_collect_records_aggregates_waivers_and_dedupes_findings():
    _, tctxs = _run_session(_floor_cfg(5.0))
    ctx = SimpleNamespace()
    collect_wash_sale_decision_records(ctx, tctxs)
    records = ctx.wash_sale_decision_records
    assert [r["ticker"] for r in records if r.get("waived")] == ["TINY"]

    # Findings are stamped on EVERY tc (config is per-run) — deduped to one.
    _, tctxs_bad = _run_session(_floor_cfg(100.0))
    ctx_bad = SimpleNamespace()
    collect_wash_sale_decision_records(ctx_bad, tctxs_bad)
    findings = [
        r for r in ctx_bad.wash_sale_decision_records
        if r.get("record") == "config_validation_finding"
    ]
    assert len(findings) == 1


def test_collect_records_is_inert_at_the_default():
    _, tctxs = _run_session(BASE_CFG)
    ctx = SimpleNamespace()
    collect_wash_sale_decision_records(ctx, tctxs)
    assert not hasattr(ctx, "wash_sale_decision_records")


def test_runtime_payload_carries_waiver_records_and_is_inert_without_them():
    from renquant_pipeline.inference import (
        InferenceContext as RuntimeInferenceContext,
    )
    from renquant_pipeline.inference import runtime_inference_payload

    def _ctx():
        return RuntimeInferenceContext(
            strategy_config={"watchlist": ["TINY"]},
            data_manifest={},
            artifact_manifest={},
            market_snapshot={"as_of": "2026-07-30"},
            decision_trace=[{"ticker": "TINY", "blocked_by": None}],
        )

    baseline = runtime_inference_payload(_ctx())
    again = runtime_inference_payload(_ctx())
    # Inert: without the attribute the payload is byte-identical.
    assert json.dumps(baseline, sort_keys=True, default=str) \
        == json.dumps(again, sort_keys=True, default=str)

    ctx = _ctx()
    record = {
        "gate": "wash_sale", "ticker": "TINY", "waived": True,
        "est_foregone_tax_usd": 0.58, "floor_usd": 5.0,
        "config_fingerprint": "sha256:0123456789abcdef",
    }
    ctx.wash_sale_decision_records = [record]
    payload = runtime_inference_payload(ctx)
    assert payload["decision_trace"] == [
        {"ticker": "TINY", "blocked_by": None},
        record,
    ]


def test_live_context_snapshot_appends_records_after_the_explicit_trace():
    from renquant_pipeline.inference import (
        live_context_snapshot_from_live_context,
    )
    row = {"ticker": "TINY", "blocked_by": None}
    record = {
        "gate": "wash_sale", "ticker": "TINY", "waived": True,
        "est_foregone_tax_usd": 0.58, "floor_usd": 5.0,
        "config_fingerprint": "sha256:0123456789abcdef",
    }
    ctx = SimpleNamespace(
        config={"watchlist": ["TINY"]},
        market_snapshot={"as_of": "2026-07-30"},
        account_snapshot={},
        decision_trace=[row],
        orders=[],
        blocked_by={},
        wash_sale_decision_records=[record],
    )
    snap = live_context_snapshot_from_live_context(ctx)
    assert snap.decision_trace == [row, record]
    # Without the attribute: verbatim explicit trace, nothing appended.
    ctx_plain = SimpleNamespace(
        config={"watchlist": ["TINY"]},
        market_snapshot={"as_of": "2026-07-30"},
        account_snapshot={},
        decision_trace=[row],
        orders=[],
        blocked_by={},
    )
    assert live_context_snapshot_from_live_context(
        ctx_plain).decision_trace == [row]


# ── 7. Downstream buy-path sites honor the SAME waiver (no re-block) ─────────


def test_waiver_helper_matches_the_gate_arithmetic():
    assert wash_sale_materiality_floor_waives(
        floor_usd=5.0, assumed_marginal_rate=0.40,
        event_net_realized_pl_usd=-1.43) is True
    assert wash_sale_materiality_floor_waives(
        floor_usd=5.0, assumed_marginal_rate=0.40,
        event_net_realized_pl_usd=-488.43) is False
    # unavailable never waives; floor<=0 never waives (defense-in-depth —
    # callers short-circuit before ever calling with floor 0).
    assert wash_sale_materiality_floor_waives(
        floor_usd=5.0, assumed_marginal_rate=0.40,
        event_net_realized_pl_usd=None) is False
    assert wash_sale_materiality_floor_waives(
        floor_usd=0.0, assumed_marginal_rate=0.40,
        event_net_realized_pl_usd=-0.01) is False


def test_qp_wash_mask_honors_the_floor_and_is_inert_at_zero():
    np = pytest.importorskip("numpy")  # noqa: F841
    from renquant_pipeline.kernel.portfolio_qp.tasks import (
        _compute_qp_wash_mask,
    )

    def _mask(**kw):
        mask, n_wash, _, _ = _compute_qp_wash_mask(
            tickers=["TINY", "BIGL"],
            today=TODAY,
            last_sell_dates=dict(LAST_SELL_DATES),
            last_sell_pls=dict(LAST_SELL_PLS),
            wash_days=30,
            min_reentry=0,
            held_tickers=set(),
            calibrator_saturated=False,
            **kw,
        )
        return list(mask), n_wash

    # Baseline (no floor args — the pre-change call shape) vs floor=0.0:
    # byte-identical mask.
    assert _mask() == _mask(materiality_floor_usd=0.0)
    assert _mask() == ([True, True], 2)
    # floor=5.0: TINY waived (Δw free), BIGL still masked.
    assert _mask(
        materiality_floor_usd=5.0, assumed_marginal_rate=0.40,
    ) == ([False, True], 1)


def test_rotation_validate_pairs_honors_the_floor():
    from renquant_pipeline.kernel.rotation import RotationPair
    from renquant_pipeline.kernel.pipeline.task_rotation import (
        ValidatePairsTask,
    )

    def _pair(buy):
        return RotationPair(
            sell_ticker="OLD", buy_ticker=buy,
            sell_score=0.3, buy_score=0.6, sell_er=0.0, buy_er=0.05,
            horizon_days=20, raw_advantage=0.05, tax_drag=0.0,
            transaction_cost=0.001, net_advantage=0.049,
            threshold=0.02, margin_realized=0.029,
        )

    def _run(cfg):
        ctx = SimpleNamespace(
            config=cfg, today=TODAY, holdings={}, corr_matrix={},
            last_sell_dates=dict(LAST_SELL_DATES),
            last_sell_pls=dict(LAST_SELL_PLS),
            rotations=[_pair("TINY"), _pair("BIGL")],
        )
        ValidatePairsTask().run(ctx)
        return [p.buy_ticker for p in ctx.rotations]

    # Default: both loss names re-blocked on the rotation buy leg.
    assert _run(BASE_CFG) == []
    assert _run(_floor_cfg(0.0)) == []
    # floor=5.0: TINY's waiver holds through the rotation leg; BIGL stands.
    assert _run(_floor_cfg(5.0)) == ["TINY"]


def test_joint_actions_honor_the_floor():
    from renquant_pipeline.context import InferenceContext
    from renquant_pipeline.kernel.selection import CandidateResult
    from renquant_pipeline.kernel.pipeline.task_joint_actions import (
        JointActionTask,
    )

    cand = CandidateResult(
        ticker="TINY", raw_score=0.5, rank_score=0.6, rs_score=0.0,
        detail="", expected_return=0.04, expected_return_horizon_days=60,
        panel_score=0.5, mu=0.04, mu_horizon_days=60, sigma=0.2,
    )

    def _cfg(extra_risk=None):
        cfg = {
            "regime_params": {"BULL_CALM": {
                "max_position_pct": 0.10, "cash_reserve_pct": 0.0,
                "max_concurrent_positions": 8,
            }},
            "ranking": {"panel_scoring": {"enabled": True, "sizing": {},
                                          "sigma_sizing": {}},
                        "kelly_sizing": {"enabled": False}},
            "regime": {},
            "rotation": {"joint_actions": {"enabled": True,
                                           "solver": "greedy"}},
            "max_positions_per_sector": 0,
            "wash_sale_days": 30,
        }
        if extra_risk is not None:
            cfg["risk"] = extra_risk
        return cfg

    def _run(cfg):
        ctx = InferenceContext(
            config=cfg, today=TODAY, regime="BULL_CALM", confidence=1.0,
            bear_only=False, portfolio_value=10_000.0, cash=10_000.0,
            prices={"TINY": 100.0}, ranked=[cand], models={},
            holdings={}, last_sell_dates=dict(LAST_SELL_DATES),
            last_sell_pls=dict(LAST_SELL_PLS),
        )
        ctx._selected = []  # noqa: SLF001
        JointActionTask().run(ctx)
        return ctx

    # Default: the trivial loss still blocks the direct-buy leg.
    ctx = _run(_cfg())
    assert ctx.counters.get("joint_blocked_wash", 0) == 1
    assert not any(o["ticker"] == "TINY" for o in ctx.orders)
    # floor=5.0: the waiver holds through the joint-action leg.
    ctx = _run(_cfg({"wash_sale": {"materiality_floor_usd": 5.0}}))
    assert ctx.counters.get("joint_blocked_wash", 0) == 0
    assert any(o["ticker"] == "TINY" for o in ctx.orders)


# ── 8. Interplay sanity: the NPV knob and this knob stay independent ─────────


def test_npv_floor_and_materiality_floor_do_not_interfere():
    """A name the NPV knob releases never reaches the materiality floor
    (it is not blocked); a name the NPV knob blocks can be waived by it."""
    cfg = {**_floor_cfg(5.0), "wash_sale_min_material_npv": 1.0}
    # TINY: NPV ≈ $0.04 < $1.00 → NPV knob releases pre-floor → PASS with no
    # waiver record (nothing was blocked, nothing was waived).
    tc = _tc("TINY", cfg)
    assert WashSaleFilterTask().run(tc) is None
    assert getattr(tc, "wash_sale_waiver", None) is None
    # BIGL: NPV ≈ $13.62 ≥ $1.00 → blocked by the gate → est $195.38 > $5.00
    # → the block stands.
    tc = _tc("BIGL", cfg)
    assert WashSaleFilterTask().run(tc) is False
