"""Tests for the M5/R1 observe-only admission shadow logger.

Covers the contract from the unified-107 master plan (Term TC row M5;
lineage: 104-capability-program §3 R1):

* delta correctness on the two fixture scenarios that motivated R1
  (name panel-scorable but tournament-stale = the June 2026 freeze; name
  tournament-admitted but stale-featured);
* funnel-noise suppression (non-panel buy gates must not mark a name
  panel-inadmissible);
* fail-isolation (an exception inside the logger NEVER fails the run —
  it is swallowed and counted);
* append-only JSONL with a self-describing schema;
* ZERO behavior change to the live admission / decision state (regression
  pin);
* the panel fail-closed day is honestly an empty panel-admission set;
* the kill switch (admission_shadow.enabled=false);
* the pp_inference wiring (hook fires after PanelScoringJob only).
"""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.task_admission_shadow import (
    MEASURED_BASES,
    PROXY_BASES,
    SCHEMA_VERSION,
    TAXONOMY_VERSION,
    AdmissionShadowLoggerTask,
    TaxonomyVersionMismatchError,
    build_acceptance_packet,
)

TODAY = datetime.date(2026, 7, 2)


def _ohlcv_frame(last_bar: datetime.date, n_bars: int = 30) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(last_bar), periods=n_bars)
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0},
        index=idx,
    )


def _ctx(tmp_path: Path, **overrides) -> InferenceContext:
    """A kernel-shaped ctx mirroring the post-PanelScoringJob live state."""
    config = {
        "watchlist": ["FRESH_ADMITTED", "FROZEN_TOURN", "NO_ARTIFACT",
                      "STALE_FEATURED", "WASH_SALE_BLOCKED"],
        "model_staleness_days": 28,
        "_strategy_dir": str(tmp_path),
        "_universe_rejections": {
            "FROZEN_TOURN": "stale_61d_limit_28:train_end",
            "NO_ARTIFACT": "no_artifact",
        },
    }
    config.update(overrides.pop("config_extra", {}))
    ctx = InferenceContext(config=config, today=TODAY)
    # Tournament-admitted set (LoadUniverseJob outcome → ctx.models).
    ctx.models = {
        "FRESH_ADMITTED": {"_metadata": {}},
        "STALE_FEATURED": {"_metadata": {}},
        "WASH_SALE_BLOCKED": {"_metadata": {}},
    }
    # Panel cross-section: FRESH_ADMITTED scored; FROZEN_TOURN scored too
    # (the R1 world where the panel covers it despite tournament staleness).
    ctx._panel_scores_all = {"FRESH_ADMITTED": 0.42, "FROZEN_TOURN": 0.17}
    # STALE_FEATURED reached the panel and measurably failed feature build;
    # WASH_SALE_BLOCKED was dropped by a NON-panel buy gate.
    ctx._blocked_by_ticker = {
        "STALE_FEATURED": "panel_score_missing",
        "WASH_SALE_BLOCKED": "wash_sale",
    }
    # OHLCV: fresh for everything except STALE_FEATURED (40d behind);
    # NO_ARTIFACT is fresh → proxy-admissible (the tournament rejected it
    # upstream so it never reached the panel).
    ctx.ohlcv = {
        "FRESH_ADMITTED": _ohlcv_frame(TODAY),
        "FROZEN_TOURN": _ohlcv_frame(TODAY),
        "NO_ARTIFACT": _ohlcv_frame(TODAY - datetime.timedelta(days=1)),
        "STALE_FEATURED": _ohlcv_frame(TODAY - datetime.timedelta(days=40)),
        "WASH_SALE_BLOCKED": _ohlcv_frame(TODAY),
    }
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _log_path(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "admission_shadow.jsonl"


def _read_records(tmp_path: Path) -> list[dict]:
    lines = _log_path(tmp_path).read_text().splitlines()
    return [json.loads(line) for line in lines]


# ── Delta correctness ─────────────────────────────────────────────────────────

def test_june_freeze_scenario_panel_scored_but_tournament_stale(tmp_path):
    """FROZEN_TOURN: panel scores it, tournament rejected it → added."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert "FROZEN_TOURN" in record["added"]
    entry = record["reasons"]["FROZEN_TOURN"]
    assert entry["side"] == "added"
    assert entry["tournament"] == "stale_61d_limit_28:train_end"
    assert entry["panel_basis"] == "panel_scored"
    assert entry["measured"] is True
    # A real measured panel-admit belongs in the R1 headline metric.
    assert "FROZEN_TOURN" in record["added_measured"]


def test_added_via_input_freshness_proxy_when_never_reached_panel(tmp_path):
    """NO_ARTIFACT: tournament-rejected, never panel-scored, inputs fresh →
    added on the proxy basis (recorded as inferred, not measured)."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert "NO_ARTIFACT" in record["added"]
    entry = record["reasons"]["NO_ARTIFACT"]
    assert entry["tournament"] == "no_artifact"
    assert entry["panel_basis"] == "input_fresh_proxy"
    assert entry["measured"] is False
    assert entry["panel"].startswith("panel_input_fresh_lag_")
    assert record["n_panel_proxy"] >= 1
    # Proxy evidence must NOT contaminate the measured-only headline metric.
    assert "NO_ARTIFACT" not in record["added_measured"]
    assert "NO_ARTIFACT" in record["added_proxy"]


def test_stale_featured_name_dropped_with_lag_reason(tmp_path):
    """STALE_FEATURED: tournament admits it but the panel measurably failed it
    (and its inputs are 40d stale) → dropped."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert "STALE_FEATURED" in record["dropped"]
    entry = record["reasons"]["STALE_FEATURED"]
    assert entry["side"] == "dropped"
    assert entry["tournament"] == "admitted"
    assert entry["panel_basis"] == "panel_block"
    assert entry["measured"] is True
    assert entry["panel"] == "panel_score_missing"
    assert "STALE_FEATURED" in record["dropped_measured"]


def test_stale_inputs_without_panel_verdict_dropped_by_lag(tmp_path):
    """A tournament-admitted name that never reached the panel AND has stale
    OHLCV drops on the input-freshness basis with the measured lag."""
    ctx = _ctx(tmp_path)
    del ctx._blocked_by_ticker["STALE_FEATURED"]   # no measured panel verdict
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    entry = record["reasons"]["STALE_FEATURED"]
    assert entry["panel_basis"] == "input_freshness"
    assert entry["measured"] is False
    assert entry["panel"].startswith("panel_input_stale_lag_")
    lag = int(entry["panel"].removeprefix("panel_input_stale_lag_").rstrip("d"))
    assert lag >= 40   # last bar pinned 40 calendar days behind the session
    # Proxy-basis drop must NOT count toward the measured headline metric.
    assert "STALE_FEATURED" not in record["dropped_measured"]
    assert "STALE_FEATURED" in record["dropped_proxy"]


def test_non_panel_buy_gate_is_not_admission_noise(tmp_path):
    """WASH_SALE_BLOCKED: dropped by a decision-funnel gate, inputs fresh →
    counts panel-admissible (NOT in dropped) — funnel outcomes must not
    flood the R1 delta."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert "WASH_SALE_BLOCKED" not in record["dropped"]
    assert "WASH_SALE_BLOCKED" not in record["added"]   # both sets admit it


def test_counts_are_consistent(tmp_path):
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert record["n_watchlist"] == 5
    assert record["n_tournament"] == 3
    # panel-admissible: FRESH_ADMITTED + FROZEN_TOURN (scored),
    # NO_ARTIFACT + WASH_SALE_BLOCKED (fresh-input proxy).
    assert record["n_panel"] == 4
    assert record["n_panel_scored"] == 2
    assert record["n_panel_proxy"] == 2
    assert record["n_intersection"] == 2
    assert sorted(record["added"]) == ["FROZEN_TOURN", "NO_ARTIFACT"]
    assert record["dropped"] == ["STALE_FEATURED"]
    # Measured/proxy split: FROZEN_TOURN is a real panel-admit (measured),
    # NO_ARTIFACT is proxy-inferred (never reached the panel); STALE_FEATURED
    # drops on a measured panel-block reason (panel_score_missing).
    assert record["added_measured"] == ["FROZEN_TOURN"]
    assert record["added_proxy"] == ["NO_ARTIFACT"]
    assert record["dropped_measured"] == ["STALE_FEATURED"]
    assert record["dropped_proxy"] == []
    assert record["n_added_measured"] == 1
    assert record["n_added_proxy"] == 1
    assert record["n_dropped_measured"] == 1
    assert record["n_dropped_proxy"] == 0
    assert record["taxonomy_version"] == TAXONOMY_VERSION


def test_missing_ohlcv_frame_fails_closed_for_proxy(tmp_path):
    ctx = _ctx(tmp_path)
    del ctx._blocked_by_ticker["STALE_FEATURED"]
    del ctx.ohlcv["STALE_FEATURED"]
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    entry = record["reasons"]["STALE_FEATURED"]
    assert entry["panel"] == "panel_input_missing_ohlcv"
    assert "STALE_FEATURED" in record["dropped"]


def test_held_flag_recorded(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.holdings = {"STALE_FEATURED": object()}
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert record["reasons"]["STALE_FEATURED"]["held"] is True
    assert record["reasons"]["FROZEN_TOURN"]["held"] is False


# ── Panel fail-closed day ─────────────────────────────────────────────────────

def test_panel_fail_closed_day_is_empty_panel_set(tmp_path):
    ctx = _ctx(tmp_path)
    ctx._panel_scoring_contract_failed = True
    ctx._panel_scoring_fail_reason = "panel_scorer_config_mismatch"
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    assert record["panel_state"] == "fail_closed"
    assert record["panel_fail_reason"] == "panel_scorer_config_mismatch"
    assert record["n_panel"] == 0
    assert record["added"] == []
    assert sorted(record["dropped"]) == sorted(ctx.models)
    for name in record["dropped"]:
        assert record["reasons"][name]["panel"] == (
            "panel_fail_closed:panel_scorer_config_mismatch"
        )
        # panel_fail_closed is a real observed operational state (the panel
        # genuinely did fail closed) -- counts as measured, not proxy.
        assert record["reasons"][name]["measured"] is True
    assert sorted(record["dropped_measured"]) == sorted(ctx.models)
    assert record["dropped_proxy"] == []


# ── Fail isolation ────────────────────────────────────────────────────────────

def test_logger_exception_never_fails_the_run_and_is_counted(tmp_path):
    """Unwritable sink (path points at a directory) → swallowed + counted."""
    sink = tmp_path / "not_a_file"
    sink.mkdir()
    ctx = _ctx(tmp_path, config_extra={"admission_shadow": {"path": str(sink)}})

    AdmissionShadowLoggerTask().run(ctx)   # must not raise

    assert ctx.counters.get("admission_shadow_errors") == 1
    assert "admission_shadow_logged" not in ctx.counters


def test_internal_exception_is_isolated(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        AdmissionShadowLoggerTask,
        "_build_record",
        lambda self, ctx: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    AdmissionShadowLoggerTask().run(ctx)   # must not raise
    assert ctx.counters.get("admission_shadow_errors") == 1
    assert not _log_path(tmp_path).exists()


# ── Append-only JSONL schema ──────────────────────────────────────────────────

REQUIRED_KEYS = {
    "schema", "taxonomy_version", "date", "broker", "run_mode", "regime",
    "panel_state", "panel_fail_reason", "n_watchlist", "n_tournament",
    "n_panel", "n_panel_scored", "n_panel_proxy", "n_intersection",
    "added", "dropped",
    "added_measured", "dropped_measured",
    "n_added_measured", "n_dropped_measured",
    "added_proxy", "dropped_proxy",
    "n_added_proxy", "n_dropped_proxy",
    "reasons",
}


def test_append_only_jsonl_schema(tmp_path):
    ctx1 = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx1)
    first_line = _log_path(tmp_path).read_text().splitlines()[0]

    ctx2 = _ctx(tmp_path)
    ctx2.today = TODAY + datetime.timedelta(days=1)
    AdmissionShadowLoggerTask().run(ctx2)

    lines = _log_path(tmp_path).read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line          # append, never rewrite
    for line in lines:
        record = json.loads(line)
        assert record["schema"] == SCHEMA_VERSION
        assert REQUIRED_KEYS <= set(record)
    assert json.loads(lines[0])["date"] == "2026-07-02"
    assert json.loads(lines[1])["date"] == "2026-07-03"
    assert ctx1.counters["admission_shadow_logged"] == 1


def test_kill_switch_disables_logging(tmp_path):
    ctx = _ctx(
        tmp_path, config_extra={"admission_shadow": {"enabled": False}},
    )
    AdmissionShadowLoggerTask().run(ctx)
    assert not _log_path(tmp_path).exists()
    assert "admission_shadow_logged" not in ctx.counters


# ── Zero behavior change (regression pin) ─────────────────────────────────────

def test_observe_only_zero_behavior_change(tmp_path):
    """The logger must not mutate ANY decision state — the live admission
    still rules. Pins the M5 contract."""
    ctx = _ctx(tmp_path)
    ctx.candidates = [{"ticker": "FRESH_ADMITTED"}]
    before = {
        "models": copy.deepcopy(ctx.models),
        "panel_scores": dict(ctx._panel_scores_all),
        "blocked": dict(ctx._blocked_by_ticker),
        "candidates": copy.deepcopy(ctx.candidates),
        "exits": list(ctx.exits),
        "orders": list(ctx.orders),
        "buy_blocked": ctx.buy_blocked,
        "skip_buys": ctx.skip_buys,
        "rejections": dict(ctx.config["_universe_rejections"]),
        "watchlist": list(ctx.config["watchlist"]),
    }

    AdmissionShadowLoggerTask().run(ctx)

    assert ctx.models == before["models"]
    assert ctx._panel_scores_all == before["panel_scores"]
    assert ctx._blocked_by_ticker == before["blocked"]
    assert ctx.candidates == before["candidates"]
    assert ctx.exits == before["exits"]
    assert ctx.orders == before["orders"]
    assert ctx.buy_blocked == before["buy_blocked"]
    assert ctx.skip_buys == before["skip_buys"]
    assert ctx.config["_universe_rejections"] == before["rejections"]
    assert ctx.config["watchlist"] == before["watchlist"]
    # Only its own telemetry counters may appear.
    assert set(ctx.counters) <= {"admission_shadow_logged",
                                 "admission_shadow_errors"}


def test_does_not_create_blocked_map_when_absent(tmp_path):
    ctx = _ctx(tmp_path)
    del ctx._blocked_by_ticker
    AdmissionShadowLoggerTask().run(ctx)
    assert not hasattr(ctx, "_blocked_by_ticker")
    assert not hasattr(ctx, "blocked_by")
    assert _log_path(tmp_path).exists()


def test_noop_on_empty_ctx(tmp_path):
    """No watchlist + no models → nothing comparable → no record, no error."""
    ctx = InferenceContext(config={"_strategy_dir": str(tmp_path)}, today=TODAY)
    AdmissionShadowLoggerTask().run(ctx)
    assert not _log_path(tmp_path).exists()
    assert ctx.counters == {}


# ── Lifted load_scorer ctx shape (panel_scores / blocked_by) ─────────────────

def test_load_scorer_ctx_shape_supported(tmp_path):
    ctx = _ctx(tmp_path)
    scores = dict(ctx._panel_scores_all)
    blocked = dict(ctx._blocked_by_ticker)
    del ctx._panel_scores_all
    del ctx._blocked_by_ticker
    ctx.panel_scores = scores
    ctx.blocked_by = blocked

    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)
    assert sorted(record["added"]) == ["FROZEN_TOURN", "NO_ARTIFACT"]
    assert record["dropped"] == ["STALE_FEATURED"]


def test_non_finite_scores_are_not_admissible_evidence(tmp_path):
    ctx = _ctx(tmp_path)
    ctx._panel_scores_all["FROZEN_TOURN"] = float("nan")
    # NaN score + no fresh-input fallback → not admissible.
    ctx.ohlcv["FROZEN_TOURN"] = _ohlcv_frame(TODAY - datetime.timedelta(days=30))
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)
    assert "FROZEN_TOURN" not in record["added"]


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_pp_inference_hooks_shadow_logger_after_panel_scoring():
    """The hook must live inside the post-PanelScoringJob block of the daily
    InferencePipeline (source-level pin, same style as the load_scorer
    routing test in test_lift_pp_inference.py)."""
    import inspect

    from renquant_pipeline.kernel.pipeline import pp_inference

    src = inspect.getsource(pp_inference.InferencePipeline.run)
    panel_block = src.split('if type(job).__name__ == "PanelScoringJob":', 1)
    assert len(panel_block) == 2, "PanelScoringJob post-block missing"
    assert "AdmissionShadowLoggerTask().run(ctx)" in panel_block[1]
    # Observe-only: it must run after the decision-affecting post-panel tasks.
    assert panel_block[1].index("DataIntegrityTask().run(ctx)") < \
        panel_block[1].index("AdmissionShadowLoggerTask().run(ctx)")


def test_sell_only_pipeline_has_no_shadow_hook():
    """Panel-based admission is undefined without panel scoring — the
    sell-only path must not log."""
    import inspect

    from renquant_pipeline.kernel.pipeline import pp_inference

    src = inspect.getsource(pp_inference.SellOnlyPipeline.run)
    assert "AdmissionShadowLoggerTask" not in src


# ── Measured vs proxy taxonomy (R1 review directive, 2026-07-02) ────────────

def test_measured_and_proxy_bases_partition_all_bases():
    """Every basis the classifier can emit is in exactly one bucket."""
    assert MEASURED_BASES == {"panel_scored", "panel_block", "panel_fail_closed"}
    assert PROXY_BASES == {"input_fresh_proxy", "input_freshness"}
    assert MEASURED_BASES.isdisjoint(PROXY_BASES)


def test_every_reason_entry_basis_is_classified(tmp_path):
    """No basis value escapes MEASURED_BASES ∪ PROXY_BASES — a silent third
    category would break both the headline metric and the histograms."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)
    for entry in record["reasons"].values():
        basis = entry["panel_basis"]
        assert basis in MEASURED_BASES or basis in PROXY_BASES
        assert entry["measured"] == (basis in MEASURED_BASES)


# ── R1 acceptance packet: frozen 20-session analysis ─────────────────────────

def test_acceptance_packet_headline_is_measured_only(tmp_path):
    """The pooled headline delta must come only from *_measured counts —
    proxy volume must never inflate it."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    packet = build_acceptance_packet([record])

    assert packet["headline"]["total_dropped_measured"] == \
        record["n_dropped_measured"]
    assert packet["headline"]["total_added_measured"] == \
        record["n_added_measured"]
    assert packet["proxy"]["total_dropped_proxy"] == record["n_dropped_proxy"]
    assert packet["proxy"]["total_added_proxy"] == record["n_added_proxy"]


def test_acceptance_packet_denominators_are_pooled_sums(tmp_path):
    """Denominators sum across sessions rather than averaging per-session
    percentages, so the packet stays comparable across a shrinking/growing
    universe within the shadow window."""
    ctx1 = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx1)
    ctx2 = _ctx(tmp_path)
    ctx2.today = TODAY + datetime.timedelta(days=1)
    AdmissionShadowLoggerTask().run(ctx2)
    records = _read_records(tmp_path)

    packet = build_acceptance_packet(records)

    assert packet["n_sessions"] == 2
    assert packet["denominators"]["total_tournament"] == sum(
        r["n_tournament"] for r in records
    )
    assert packet["denominators"]["total_panel_measured_admissible"] == sum(
        r["n_panel_scored"] for r in records
    )
    assert packet["headline"]["dropped_measured_rate"] == pytest.approx(
        sum(r["n_dropped_measured"] for r in records)
        / packet["denominators"]["total_tournament"]
    )


def test_acceptance_packet_reason_histograms_split_measured_and_proxy(
    tmp_path,
):
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)

    packet = build_acceptance_packet([record])

    assert "panel_scored" in packet["reason_histogram_measured"]
    assert "panel_block" in packet["reason_histogram_measured"]
    assert "input_fresh_proxy" in packet["reason_histogram_proxy"]
    # No basis should appear in both histograms.
    assert set(packet["reason_histogram_measured"]).isdisjoint(
        packet["reason_histogram_proxy"]
    )


def test_acceptance_packet_rejects_mismatched_taxonomy_version(tmp_path):
    """A record from a different taxonomy version must not be silently
    pooled — the basis categories/denominators could mean something
    different under an old version."""
    ctx = _ctx(tmp_path)
    AdmissionShadowLoggerTask().run(ctx)
    (record,) = _read_records(tmp_path)
    stale_record = dict(record, taxonomy_version="admission_shadow_taxonomy.v0")

    with pytest.raises(TaxonomyVersionMismatchError):
        build_acceptance_packet([record, stale_record])


def test_acceptance_packet_empty_window():
    packet = build_acceptance_packet([])
    assert packet["n_sessions"] == 0
    assert packet["taxonomy_version"] == TAXONOMY_VERSION
    assert packet["headline"] == {}
