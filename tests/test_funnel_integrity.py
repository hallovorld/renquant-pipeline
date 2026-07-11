"""Tests for the FUNNEL-INTEGRITY pipeline step (task_funnel_integrity).

Covers the operator mandate ("pipeline 要加步骤来彻底解决 the silent-no-buy
class"):

* each structural detector fires on a synthetic context reproducing its
  incident signature:
    - the 2026-07-08/09 admission-staleness collapse
      (``stale_76d_limit_60:live_train_end`` × 133/145 → buy scan on 0
      tickers, mis-reported as a normal no-trade);
    - the shadow config-FP fail-close clear (panel_scorer_config_mismatch
      → all candidates cleared → "no trade" that was a CONTRACT failure);
    - the threshold-vs-scale mismatch from the PatchTST-negative-scores era
      (absolute conviction floor above an all-negative mu scale);
    - the wash-sale mass-block anomaly (STATE-EXT-SELL date bug);
    - the whole-funnel single-gate kill;
    - zero-priced / missing-data candidates;
* clean sessions classify ECONOMIC_NO_TRADE / ECONOMIC_TRADE with nothing
  fired;
* partial suppression classifies DEGRADED;
* fail isolation: a detector exception, and a whole-task exception, never
  fail the run — the integrity block carries the error;
* ZERO behavior change to decision state (regression pin);
* the kill switch (funnel_integrity.enabled=false);
* rolling history maintenance on ctx.monitor_state;
* the notification contract (OUTAGE vs no-trade title fields);
* the pp_inference wiring (end of InferencePipeline only, never
  SellOnlyPipeline).
"""
from __future__ import annotations

import copy
import datetime
import inspect
from types import SimpleNamespace

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.task_funnel_integrity import (
    CTX_ATTR,
    DEFAULT_INVARIANTS,
    HISTORY_STATE_KEY,
    SCHEMA_VERSION,
    SEVERITY_STRUCTURAL,
    SEVERITY_WARN,
    VERDICT_DEGRADED,
    VERDICT_ECONOMIC_NO_TRADE,
    VERDICT_ECONOMIC_TRADE,
    VERDICT_STRUCTURAL_BLOCK,
    FunnelIntegrityTask,
    InvariantFinding,
    build_funnel_view,
    gate_family,
    notification_headline,
)

TODAY = datetime.date(2026, 7, 8)


def _cand(ticker: str, er: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(ticker=ticker, expected_return=er)


def _ctx(**overrides) -> InferenceContext:
    config = {
        "watchlist": ["AAA", "BBB", "CCC", "DDD", "EEE"],
        "_universe_rejections": {},
    }
    config.update(overrides.pop("config_extra", {}))
    ctx = InferenceContext(config=config, today=TODAY)
    ctx._run_mode = "full"
    ctx.models = {t: {"_metadata": {}} for t in config["watchlist"]}
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _block(ctx) -> dict:
    block = getattr(ctx, CTX_ATTR, None)
    assert isinstance(block, dict), "funnel_integrity block missing on ctx"
    assert block["schema"] == SCHEMA_VERSION
    return block


def _fired_names(block: dict) -> set[str]:
    return {f["invariant"] for f in block["fired"]}


# ── Incident signature: 2026-07-08/09 admission-staleness collapse ───────────

def test_universe_admission_collapse_fires_on_0708_signature():
    watchlist = [f"T{i:03d}" for i in range(145)]
    admitted = watchlist[:12]
    rejections = {
        t: "stale_76d_limit_60:live_train_end" for t in watchlist[12:]
    }
    ctx = _ctx(config_extra={
        "watchlist": watchlist,
        "_universe_rejections": rejections,
    })
    ctx.models = {t: {"_metadata": {}} for t in admitted}

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "universe_admission_collapse" in _fired_names(block)
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK
    assert block["structural"] is True
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "universe_admission_collapse"
    )
    assert finding["severity"] == SEVERITY_STRUCTURAL
    assert finding["evidence"]["n_watchlist"] == 145
    assert finding["evidence"]["n_admitted"] == 12
    assert finding["evidence"]["n_staleness_rejections"] == 133
    assert finding["evidence"]["admitted_below_floor"] is True
    assert finding["evidence"]["staleness_above_threshold"] is True
    # The headline must title this an OUTAGE, never a no-trade.
    headline = notification_headline(block)
    assert headline["outage"] is True
    assert headline["title_tag"] == "OUTAGE"
    # Counter mirrors for counters_json persistence.
    assert ctx.counters["funnel_integrity_structural"] == 1
    assert ctx.counters["funnel_integrity_fired"] >= 1


def test_universe_admission_collapse_quiet_on_healthy_universe():
    ctx = _ctx()   # full watchlist admitted, no rejections
    FunnelIntegrityTask().run(ctx)
    assert "universe_admission_collapse" not in _fired_names(_block(ctx))


# ── Incident signature: config-FP fail-close clear ───────────────────────────

def test_fail_close_event_fires_on_config_fp_clear():
    ctx = _ctx()
    ctx._panel_scoring_contract_failed = True
    ctx._panel_scoring_fail_reason = "panel_scorer_config_mismatch"
    ctx._blocked_by_ticker = {
        "AAA": "panel_scorer_config_mismatch",
        "BBB": "panel_scorer_config_mismatch",
    }
    ctx.candidates = []   # the fail-close cleared everything

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "fail_close_event" in _fired_names(block)
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK
    finding = next(
        f for f in block["fired"] if f["invariant"] == "fail_close_event"
    )
    assert finding["severity"] == SEVERITY_STRUCTURAL
    assert finding["evidence"]["panel_fail_closed"] is True
    assert finding["evidence"]["panel_fail_reason"] == (
        "panel_scorer_config_mismatch"
    )
    assert finding["evidence"]["fail_close_clears"] == {
        "panel_scorer_config_mismatch": 2,
    }


def test_fail_close_event_fires_on_calibrator_flag_alone():
    ctx = _ctx()
    ctx._calibrator_contract_failed = True
    FunnelIntegrityTask().run(ctx)
    block = _block(ctx)
    assert "fail_close_event" in _fired_names(block)


# ── Incident signature: PatchTST-negative-scores threshold mismatch ──────────

def test_threshold_scale_mismatch_structural_on_all_negative_mus():
    ctx = _ctx(config_extra={
        "ranking": {"panel_scoring": {"conviction_gate": {
            "enabled": True, "mu_floor": 0.03,
        }}},
    })
    # PatchTST era: every calibrated mu negative; floor 0.03 unreachable
    # by construction — the structural sell-only failure.
    ctx._ticker_score_snapshot = {
        "AAA": {"expected_return": -0.198},
        "BBB": {"expected_return": -0.054},
        "CCC": {"expected_return": -0.011},
    }
    ctx.candidates = []

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "threshold_scale_mismatch" in _fired_names(block)
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "threshold_scale_mismatch"
    )
    assert finding["severity"] == SEVERITY_STRUCTURAL
    conviction = finding["evidence"]["checks"]["conviction"]
    assert conviction["mu_floor"] == pytest.approx(0.03)
    assert conviction["max_mu"] == pytest.approx(-0.011)
    assert conviction["all_mus_nonpositive"] is True


def test_threshold_scale_mismatch_warn_when_floor_above_positive_max():
    ctx = _ctx(config_extra={
        "ranking": {"panel_scoring": {"conviction_gate": {
            "enabled": True, "mu_floor": 0.03,
        }}},
    })
    ctx._ticker_score_snapshot = {
        "AAA": {"expected_return": 0.0192},   # the 07-06 META number
        "BBB": {"expected_return": 0.0104},
    }
    ctx.candidates = []

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "threshold_scale_mismatch"
    )
    assert finding["severity"] == SEVERITY_WARN
    # warn-only + zero buys = DEGRADED (partial), not a full OUTAGE.
    assert block["verdict"] == VERDICT_DEGRADED
    assert notification_headline(block)["title_tag"] == "DEGRADED"


def test_threshold_scale_mismatch_quiet_when_floor_reachable():
    ctx = _ctx(config_extra={
        "ranking": {"panel_scoring": {"conviction_gate": {
            "enabled": True, "mu_floor": 0.03,
        }}},
    })
    ctx._ticker_score_snapshot = {
        "AAA": {"expected_return": 0.045},
        "BBB": {"expected_return": -0.01},
    }
    FunnelIntegrityTask().run(ctx)
    assert "threshold_scale_mismatch" not in _fired_names(_block(ctx))


def test_threshold_scale_mismatch_rotation_leg():
    ctx = _ctx(config_extra={
        "rotation": {"enabled": True, "min_expected_advantage_pct": 0.10},
    })
    ctx._ticker_score_snapshot = {"AAA": {"expected_return": 0.02}}
    ctx.rotations = []

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "threshold_scale_mismatch"
    )
    rotation = finding["evidence"]["checks"]["rotation"]
    assert rotation["min_expected_advantage_pct"] == pytest.approx(0.10)
    assert rotation["max_expected_return"] == pytest.approx(0.02)
    assert finding["severity"] == SEVERITY_WARN


# ── Incident signature: wash-sale mass block (STATE-EXT-SELL date bug) ───────

def _wash_history(counts: list[int]) -> list[dict]:
    return [
        {
            "date": (TODAY - datetime.timedelta(days=i + 1)).isoformat(),
            "kill_families": ["model_signal"],
            "wash_sale_blocked": c,
        }
        for i, c in enumerate(counts)
    ]


def test_wash_sale_mass_block_fires_above_historical_p99():
    ctx = _ctx()
    ctx._blocked_by_ticker = {
        f"W{i}": "wash_sale:npv_cost_exceeds_edge" for i in range(8)
    }
    ctx.monitor_state = {
        HISTORY_STATE_KEY: _wash_history([0, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]),
    }

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "wash_sale_mass_block" in _fired_names(block)
    finding = next(
        f for f in block["fired"] if f["invariant"] == "wash_sale_mass_block"
    )
    assert finding["evidence"]["wash_sale_blocked"] == 8
    assert finding["evidence"]["historical_p99"] == 1
    assert finding["evidence"]["history_basis"] == "sufficient"
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK


def test_wash_sale_mass_block_suppressed_when_history_says_normal():
    ctx = _ctx()
    ctx._blocked_by_ticker = {
        f"W{i}": "wash_sale:npv_cost_exceeds_edge" for i in range(8)
    }
    # History says 8-10 wash-sale blocks is a routine session.
    ctx.monitor_state = {
        HISTORY_STATE_KEY: _wash_history([9, 10, 8, 9, 10, 9, 8, 10, 9, 9, 10, 8]),
    }
    FunnelIntegrityTask().run(ctx)
    assert "wash_sale_mass_block" not in _fired_names(_block(ctx))


def test_wash_sale_mass_block_cold_start_uses_absolute_floor():
    ctx = _ctx()
    ctx._blocked_by_ticker = {
        f"W{i}": "wash_sale:npv_cost_exceeds_edge" for i in range(8)
    }
    FunnelIntegrityTask().run(ctx)    # no history at all
    block = _block(ctx)
    assert "wash_sale_mass_block" in _fired_names(block)
    finding = next(
        f for f in block["fired"] if f["invariant"] == "wash_sale_mass_block"
    )
    assert finding["evidence"]["history_basis"] == "insufficient"


def test_wash_sale_below_absolute_floor_never_fires():
    ctx = _ctx()
    ctx._blocked_by_ticker = {"W0": "wash_sale:blocked", "W1": "wash_sale:blocked"}
    FunnelIntegrityTask().run(ctx)
    assert "wash_sale_mass_block" not in _fired_names(_block(ctx))


# ── Whole-funnel single-gate kill ─────────────────────────────────────────────

def _rare_gate_history(n: int, family: str = "model_signal") -> list[dict]:
    return [
        {
            "date": (TODAY - datetime.timedelta(days=i + 1)).isoformat(),
            "kill_families": [family],
            "wash_sale_blocked": 0,
        }
        for i in range(n)
    ]


def test_single_gate_kill_structural_when_history_says_rare():
    ctx = _ctx()
    ctx._full_candidate_snapshot = [
        _cand("AAA", 0.05), _cand("BBB", 0.04), _cand("CCC", 0.06),
    ]
    ctx._blocked_by_ticker = {
        "AAA": "earnings_blackout",
        "BBB": "earnings_blackout",
        "CCC": "earnings_blackout",
    }
    ctx.candidates = []
    # 15 prior sessions; earnings_blackout never killed anyone.
    ctx.monitor_state = {HISTORY_STATE_KEY: _rare_gate_history(15)}

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "single_gate_funnel_kill"
    )
    assert finding["severity"] == SEVERITY_STRUCTURAL
    assert finding["evidence"]["gate_family"] == "earnings_blackout"
    assert finding["evidence"]["share"] == pytest.approx(1.0)
    assert finding["evidence"]["history_fire_rate"] == pytest.approx(0.0)
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK


def test_single_gate_kill_suppressed_when_gate_fires_routinely():
    ctx = _ctx()
    ctx._full_candidate_snapshot = [
        _cand("AAA", 0.05), _cand("BBB", 0.04), _cand("CCC", 0.06),
    ]
    ctx._blocked_by_ticker = {
        "AAA": "conviction:mu_below_floor",
        "BBB": "conviction:mu_below_floor",
        "CCC": "conviction:mu_below_floor",
    }
    ctx.candidates = []
    ctx.monitor_state = {
        HISTORY_STATE_KEY: _rare_gate_history(15, family="conviction"),
    }
    FunnelIntegrityTask().run(ctx)
    assert "single_gate_funnel_kill" not in _fired_names(_block(ctx))


def test_single_gate_kill_cold_start_downgrades_to_warn():
    ctx = _ctx()
    ctx._full_candidate_snapshot = [
        _cand("AAA", 0.05), _cand("BBB", 0.04), _cand("CCC", 0.06),
    ]
    ctx._blocked_by_ticker = {
        "AAA": "earnings_blackout",
        "BBB": "earnings_blackout",
        "CCC": "earnings_blackout",
    }
    ctx.candidates = []

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "single_gate_funnel_kill"
    )
    assert finding["severity"] == SEVERITY_WARN
    assert finding["evidence"]["history_basis"] == "insufficient"
    assert block["verdict"] == VERDICT_DEGRADED


def test_single_gate_kill_quiet_when_candidates_survive():
    ctx = _ctx()
    ctx._full_candidate_snapshot = [_cand("AAA", 0.05), _cand("BBB", 0.04)]
    ctx._blocked_by_ticker = {"AAA": "earnings_blackout"}
    ctx.candidates = [_cand("BBB", 0.04)]
    FunnelIntegrityTask().run(ctx)
    assert "single_gate_funnel_kill" not in _fired_names(_block(ctx))


# ── Zero-priced / missing-data candidates ─────────────────────────────────────

def test_zero_priced_candidates_fires():
    ctx = _ctx()
    ctx.prices = {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0, "DDD": 101.5, "EEE": 55.2}
    FunnelIntegrityTask().run(ctx)
    block = _block(ctx)
    finding = next(
        f for f in block["fired"]
        if f["invariant"] == "zero_priced_candidates"
    )
    assert finding["evidence"]["n_offenders"] == 3
    assert finding["evidence"]["sample"] == {
        "AAA": "zero_or_missing_price",
        "BBB": "zero_or_missing_price",
        "CCC": "zero_or_missing_price",
    }
    assert block["verdict"] == VERDICT_STRUCTURAL_BLOCK


def test_zero_priced_quiet_when_price_map_never_populated():
    # An adapter that doesn't fill ctx.prices/ctx.ohlcv must not fire it.
    ctx = _ctx()
    FunnelIntegrityTask().run(ctx)
    assert "zero_priced_candidates" not in _fired_names(_block(ctx))


# ── Clean-session verdicts ────────────────────────────────────────────────────

def _clean_ctx(**overrides) -> InferenceContext:
    config_extra = {
        "ranking": {"panel_scoring": {"conviction_gate": {
            "enabled": True, "mu_floor": 0.03,
        }}},
    }
    config_extra.update(overrides.pop("config_extra", {}))
    ctx = _ctx(config_extra=config_extra, **overrides)
    ctx.prices = {t: 100.0 for t in ctx.config["watchlist"]}
    ctx._full_candidate_snapshot = [
        _cand("AAA", 0.045), _cand("BBB", 0.012), _cand("CCC", 0.019),
    ]
    # Two candidates die at correctly-scaled economic bars; one survives.
    ctx._blocked_by_ticker = {
        "BBB": "conviction:mu_below_floor",
        "CCC": "veto:rank_score_below_floor",
    }
    ctx.candidates = [_cand("AAA", 0.045)]
    return ctx


def test_clean_no_trade_session_is_economic_no_trade():
    ctx = _clean_ctx()
    ctx.orders = []

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert block["fired"] == []
    assert block["structural"] is False
    assert block["verdict"] == VERDICT_ECONOMIC_NO_TRADE
    headline = notification_headline(block)
    assert headline["outage"] is False
    assert headline["title_tag"] == "NO-TRADE"
    assert block["funnel"]["n_candidates_final"] == 1
    assert block["gate_kill_counts"] == {"conviction": 1, "veto": 1}


def test_clean_traded_session_is_economic_trade():
    ctx = _clean_ctx()
    ctx.orders = [{"ticker": "AAA", "shares": 3, "price": 100.0}]
    FunnelIntegrityTask().run(ctx)
    block = _block(ctx)
    assert block["fired"] == []
    assert block["verdict"] == VERDICT_ECONOMIC_TRADE
    assert notification_headline(block)["title_tag"] == "TRADE"


def test_structural_finding_with_buys_is_degraded():
    watchlist = [f"T{i:03d}" for i in range(100)]
    ctx = _ctx(config_extra={
        "watchlist": watchlist,
        "_universe_rejections": {
            t: "stale_70d_limit_60:live_train_end" for t in watchlist[10:]
        },
    })
    ctx.models = {t: {"_metadata": {}} for t in watchlist[:10]}
    ctx.orders = [{"ticker": "T001", "shares": 1, "price": 10.0}]

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "universe_admission_collapse" in _fired_names(block)
    assert block["verdict"] == VERDICT_DEGRADED


# ── Fail isolation ────────────────────────────────────────────────────────────

class _BoomInvariant:
    name = "boom"

    def evaluate(self, view, cfg):
        raise RuntimeError("detector exploded")


def test_detector_exception_is_isolated_and_recorded():
    ctx = _clean_ctx()
    task = FunnelIntegrityTask(
        invariants=(_BoomInvariant(),) + DEFAULT_INVARIANTS,
    )

    task.run(ctx)    # must not raise

    block = _block(ctx)
    # The crash is carried on the block; the other detectors still ran and
    # the verdict is still produced.
    assert "boom" in block["error"]
    assert "RuntimeError" in block["error"]
    assert block["verdict"] == VERDICT_ECONOMIC_NO_TRADE
    assert "boom" in block["invariants_evaluated"]


def test_whole_task_exception_never_raises_and_stamps_error(monkeypatch):
    ctx = _clean_ctx()
    task = FunnelIntegrityTask()
    monkeypatch.setattr(
        task, "_build_block",
        lambda _ctx: (_ for _ in ()).throw(ValueError("total collapse")),
    )

    task.run(ctx)    # must not raise — its crash must never dark the run

    assert ctx.counters["funnel_integrity_errors"] == 1
    block = getattr(ctx, CTX_ATTR)
    assert block["verdict"] is None
    assert "total collapse" in block["error"]
    assert notification_headline(block)["title_tag"] == "UNKNOWN"


def test_zero_behavior_change_regression_pin():
    ctx = _clean_ctx()
    ctx.orders = [{"ticker": "AAA", "shares": 3, "price": 100.0}]
    before = {
        "candidates": copy.deepcopy(
            [(c.ticker, c.expected_return) for c in ctx.candidates]
        ),
        "orders": copy.deepcopy(ctx.orders),
        "exits": copy.deepcopy(ctx.exits),
        "blocked": copy.deepcopy(ctx._blocked_by_ticker),
        "buy_blocked": ctx.buy_blocked,
        "skip_buys": ctx.skip_buys,
        "models": set(ctx.models),
    }

    FunnelIntegrityTask().run(ctx)

    assert [(c.ticker, c.expected_return) for c in ctx.candidates] \
        == before["candidates"]
    assert ctx.orders == before["orders"]
    assert ctx.exits == before["exits"]
    assert ctx._blocked_by_ticker == before["blocked"]
    assert ctx.buy_blocked == before["buy_blocked"]
    assert ctx.skip_buys == before["skip_buys"]
    assert set(ctx.models) == before["models"]


# ── Kill switches ─────────────────────────────────────────────────────────────

def test_kill_switch_disables_everything():
    ctx = _clean_ctx(config_extra={"funnel_integrity": {"enabled": False}})
    FunnelIntegrityTask().run(ctx)
    assert getattr(ctx, CTX_ATTR, None) is None
    assert "funnel_integrity_fired" not in ctx.counters


def test_per_invariant_kill_switch():
    ctx = _ctx(config_extra={
        "funnel_integrity": {
            "universe_admission_collapse": {"enabled": False},
        },
        "watchlist": ["AAA", "BBB", "CCC", "DDD", "EEE"],
        "_universe_rejections": {
            t: "stale_99d_limit_60:live_train_end"
            for t in ["BBB", "CCC", "DDD", "EEE"]
        },
    })
    ctx.models = {"AAA": {"_metadata": {}}}

    FunnelIntegrityTask().run(ctx)

    block = _block(ctx)
    assert "universe_admission_collapse" not in block["invariants_evaluated"]
    assert "universe_admission_collapse" not in _fired_names(block)


def test_sell_only_run_mode_skips():
    ctx = _clean_ctx()
    ctx._run_mode = "sell-only"
    FunnelIntegrityTask().run(ctx)
    assert getattr(ctx, CTX_ATTR, None) is None


# ── Rolling history maintenance ───────────────────────────────────────────────

def test_history_appends_compact_record_and_caps_window():
    ctx = _clean_ctx(config_extra={"funnel_integrity": {"history_window": 5}})
    ctx.monitor_state = {HISTORY_STATE_KEY: _rare_gate_history(10)}

    FunnelIntegrityTask().run(ctx)

    hist = ctx.monitor_state[HISTORY_STATE_KEY]
    assert len(hist) == 5
    assert hist[-1]["date"] == TODAY.isoformat()
    assert hist[-1]["kill_families"] == ["conviction", "veto"]
    assert hist[-1]["wash_sale_blocked"] == 0
    assert hist[-1]["verdict"] == VERDICT_ECONOMIC_NO_TRADE


def test_history_same_date_rerun_replaces_not_duplicates():
    ctx = _clean_ctx()
    FunnelIntegrityTask().run(ctx)
    FunnelIntegrityTask().run(ctx)
    hist = ctx.monitor_state[HISTORY_STATE_KEY]
    assert [h["date"] for h in hist].count(TODAY.isoformat()) == 1


def test_detectors_read_prior_history_not_today():
    # Today's record must not feed back into today's evaluation.
    ctx = _ctx()
    ctx._blocked_by_ticker = {
        f"W{i}": "wash_sale:blocked" for i in range(8)
    }
    ctx.monitor_state = {HISTORY_STATE_KEY: _wash_history([0] * 12) + [{
        "date": TODAY.isoformat(),
        "kill_families": ["wash_sale"],
        "wash_sale_blocked": 999,     # a stale same-date record
    }]}
    FunnelIntegrityTask().run(ctx)
    block = _block(ctx)
    finding = next(
        f for f in block["fired"] if f["invariant"] == "wash_sale_mass_block"
    )
    assert finding["evidence"]["historical_p99"] == 0


# ── View / helpers ────────────────────────────────────────────────────────────

def test_gate_family_normalization():
    assert gate_family("wash_sale:npv_cost") == "wash_sale"
    assert gate_family("conviction:mu_below_floor") == "conviction"
    assert gate_family("veto:rank_score_below_floor") == "veto"
    assert gate_family("earnings_blackout") == "earnings_blackout"
    assert gate_family("") == "unknown"


def test_build_funnel_view_merges_both_blocked_maps():
    ctx = _ctx()
    ctx.blocked_by = {"AAA": "wash_sale"}
    ctx._blocked_by_ticker = {"BBB": "earnings_blackout"}
    view = build_funnel_view(ctx)
    assert view.blocked == {"AAA": "wash_sale", "BBB": "earnings_blackout"}
    assert view.gate_kill_counts == {"wash_sale": 1, "earnings_blackout": 1}


def test_notification_headline_contract():
    assert notification_headline(None) == {
        "outage": False,
        "title_tag": "UNKNOWN",
        "line": "funnel integrity: not evaluated",
    }
    block = {
        "schema": SCHEMA_VERSION,
        "verdict": VERDICT_STRUCTURAL_BLOCK,
        "fired": [{"invariant": "universe_admission_collapse"}],
        "error": None,
    }
    headline = notification_headline(block)
    assert headline["outage"] is True
    assert headline["title_tag"] == "OUTAGE"
    assert "universe_admission_collapse" in headline["line"]


def test_finding_as_dict_roundtrip():
    finding = InvariantFinding(
        invariant="x", severity=SEVERITY_WARN, reason="r", evidence={"k": 1},
    )
    assert finding.as_dict() == {
        "invariant": "x", "severity": "warn", "reason": "r",
        "evidence": {"k": 1},
    }


# ── pp_inference wiring ──────────────────────────────────────────────────────

def test_wired_at_end_of_inference_pipeline_only():
    from renquant_pipeline.kernel.pipeline import pp_inference

    full = inspect.getsource(pp_inference.InferencePipeline.run)
    sell_only = inspect.getsource(pp_inference.SellOnlyPipeline.run)
    assert "FunnelIntegrityTask().run(ctx)" in full
    # Must run at the very END: after the decision-ledger write.
    assert full.index("FunnelIntegrityTask().run(ctx)") \
        > full.index("DecisionLedgerWriteTask")
    # The exit-only variant has no buy funnel to judge.
    assert "FunnelIntegrityTask" not in sell_only
