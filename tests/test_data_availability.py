"""Tests for the pre-decision DATA-AVAILABILITY gate (task_data_availability).

Covers the operator mandate (one general input-integrity gate, replacing
fragmented per-input checks) — each historical incident signature fires its
axis:

* stale fundamentals SERVING axis (the serving-axis-clip class: feed frozen
  ~88d while P-FUND-FRESHNESS was structurally unsatisfiable);
* ancient panel-model train vintage (the 2026-06-26 "你的assertion不可信"
  class: model trained to 2024-11 serving silently under a soft-skip);
* whole-dataset ABSENCE (the SGOV class: a required dataset that simply does
  not exist);
* admission-model metadata coverage collapse (the 07-08/09 class: 133/145
  stale → buy scan on ~0 tickers);
* missing/required calibrator (the missing_global_calibration fail-close
  signature, caught before scoring);
* missing/stale OHLCV bars + coverage fraction;
* benchmark (regime input) absence; account snapshot absence/staleness.

Plus the gate mechanics:

* day-one CONTRACT SCOPE (Codex review, PR #187): a consumed axis with no
  reviewed contract entry is NOT evaluated — recorded as unverified (no
  freshness verdict), never alarms, never blocks;
* per-axis fail policy, enforced BUY-SIDE ONLY (Codex review, PR #187):
  fail_closed records blocked=True in run() (never raises) and is applied
  by enforce_buy_block() — called ONLY after the sell/exit pass — via
  ctx.buy_blocked; degrade_with_alarm (the day-one default for every
  declared axis) proceeds with the alarm in ctx.data_availability +
  counters;
* clean pass → verdict AVAILABLE, nothing fired, nothing raised;
* fail isolation: a checker crash under degrade never darks the run; under
  fail_closed it is recorded as blocked (an unverifiable input is a fail,
  not a pass) but STILL never raises; a whole-task crash is swallowed
  regardless of fail_closed declarations (run() never raises);
* ZERO decision-logic change to SELLS ever; buy_blocked IS mutated by
  enforce_buy_block when a fail_closed axis fires (buy-decision-affecting
  by design, not behavior-invariant);
* kill switch + sell-only skip;
* repo scope: no notification/ntfy formatting helper lives in this module;
* pp_inference wiring: run() early in InferencePipeline (before RegimeJob,
  never in SellOnlyPipeline); enforce_buy_block() AFTER the sell/exit pass,
  before the buy candidate scan — plus a full-pipeline integration test
  proving a fail_closed violation still emits a real sell/exit.
"""
from __future__ import annotations

import datetime
import inspect
import json
import sys
import types

import numpy as np
import pandas as pd
import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.exits import HoldingState
from renquant_pipeline.kernel.pipeline.task_data_availability import (
    AXIS_ERROR,
    AXIS_OK,
    AXIS_SKIP,
    AXIS_UNVERIFIED,
    AXIS_VIOLATION,
    BUILTIN_CHECKERS,
    CTX_ATTR,
    DEFAULT_CONTRACTS,
    POLICY_DEGRADE,
    POLICY_FAIL_CLOSED,
    SCHEMA_VERSION,
    VERDICT_AVAILABLE,
    VERDICT_BLOCKED,
    VERDICT_DEGRADED,
    AxisResult,
    DataAvailabilityGateTask,
)

TODAY = datetime.date(2026, 7, 10)
WATCHLIST = ["AAA", "BBB", "CCC", "DDD", "EEE"]


# ── Fixture builders ──────────────────────────────────────────────────────────

def _bars(end: datetime.date, n: int = 30) -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100},
        index=idx,
    )


def _fresh_ohlcv(symbols: list[str], end: datetime.date = TODAY) -> dict:
    return {s: _bars(end) for s in symbols}


def _fund_parquet(tmp_path, tickers: list[str], as_of: datetime.date):
    path = tmp_path / "sec_fundamentals_daily.parquet"
    df = pd.DataFrame({
        "date": [pd.Timestamp(as_of)] * len(tickers),
        "ticker": tickers,
        "value": [1.0] * len(tickers),
    })
    df.to_parquet(path)
    return path


def _panel_artifact(tmp_path, *, trained: str = "2026-06-20",
                    cutoff: str = "2026-06-15",
                    calibration: dict | None = None,
                    stamp_fingerprint: bool = True):
    doc: dict = {
        "kind": "panel_ltr_xgboost",
        "trained_date": trained,
        "effective_train_cutoff_date": cutoff,
    }
    if stamp_fingerprint:
        doc["model_content_sha256"] = "deadbeef" * 8
    if calibration is not None:
        doc["global_calibration"] = calibration
    path = tmp_path / "panel-ltr.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _fresh_models(tickers: list[str], cutoff: str = "2026-06-20") -> dict:
    return {
        t: {"_metadata": {"effective_train_cutoff_date": cutoff}}
        for t in tickers
    }


def _ctx(tmp_path=None, *, config_extra: dict | None = None,
         clean: bool = False, **overrides) -> InferenceContext:
    config: dict = {
        "watchlist": list(WATCHLIST),
        "benchmark": "SPY",
        "model_staleness_days": 60,
    }
    if clean:
        assert tmp_path is not None
        fund = _fund_parquet(tmp_path, WATCHLIST, TODAY - datetime.timedelta(days=1))
        artifact = _panel_artifact(
            tmp_path,
            calibration={"method": "linear", "slope": 1.0, "intercept": 0.0,
                         "required": True,
                         "model_content_sha256": "cafebabe" * 8},
        )
        config["ranking"] = {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}
        config["data_contracts"] = {
            "schema": "data_contracts.v1",
            "axes": {
                "ohlcv_bars": {},
                "fundamentals_serving_axis": {"path": str(fund)},
                "panel_model_artifact": {},
                "calibrator": {},
                "admission_model_metadata": {},
                "regime_inputs": {},
                "account_snapshot": {},
            },
        }
    config.update(config_extra or {})
    ctx = InferenceContext(config=config, today=TODAY)
    ctx._run_mode = "full"
    ctx.ohlcv = overrides.pop("ohlcv", _fresh_ohlcv(WATCHLIST + ["SPY"]))
    ctx.models = overrides.pop("models", _fresh_models(WATCHLIST))
    ctx.portfolio_value = overrides.pop("portfolio_value", 10_000.0)
    ctx.cash = overrides.pop("cash", 2_000.0)
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _block(ctx) -> dict:
    block = getattr(ctx, CTX_ATTR, None)
    assert isinstance(block, dict), "data_availability block missing from ctx"
    return block


def _axis(ctx, name: str) -> dict:
    return _block(ctx)["axes"][name]


def _contract(axes: dict) -> dict:
    return {"data_contracts": {"schema": "data_contracts.v1", "axes": axes}}


# ── Clean pass ────────────────────────────────────────────────────────────────

class TestCleanPass:
    def test_all_axes_ok_verdict_available(self, tmp_path):
        ctx = _ctx(tmp_path, clean=True)
        assert DataAvailabilityGateTask().run(ctx) is True
        block = _block(ctx)
        assert block["schema"] == SCHEMA_VERSION
        assert block["verdict"] == VERDICT_AVAILABLE
        assert block["degraded"] is False
        assert block["blocked"] is False
        assert block["fired"] == []
        assert block["missing_contracts"] == []
        for name in BUILTIN_CHECKERS:
            assert _axis(ctx, name)["verdict"] in (AXIS_OK, AXIS_SKIP), name
        assert ctx.counters["data_availability_fired"] == 0
        assert ctx.counters["data_availability_degraded"] == 0
        assert ctx.counters["data_availability_blocked"] == 0

    def test_every_builtin_axis_evaluated(self, tmp_path):
        ctx = _ctx(tmp_path, clean=True)
        DataAvailabilityGateTask().run(ctx)
        for name in BUILTIN_CHECKERS:
            assert name in _block(ctx)["axes"]


# ── Incident signatures ───────────────────────────────────────────────────────

class TestStaleFundamentalsServingAxis:
    """The serving-axis-clip class: feed frozen ~88d (base-data #26 bug)."""

    def test_stale_serving_axis_fires(self, tmp_path):
        fund = _fund_parquet(
            tmp_path, WATCHLIST, TODAY - datetime.timedelta(days=88))
        ctx = _ctx(config_extra=_contract(
            {"fundamentals_serving_axis": {"path": str(fund)}}))
        assert DataAvailabilityGateTask().run(ctx) is True   # degrade default
        axis = _axis(ctx, "fundamentals_serving_axis")
        assert axis["verdict"] == AXIS_VIOLATION
        assert axis["age_days"] == 88
        assert any("serving_axis_stale" in v for v in axis["violations"])
        assert any("coverage" in v for v in axis["violations"])
        assert _block(ctx)["verdict"] == VERDICT_DEGRADED

    def test_per_symbol_coverage_partial_staleness(self, tmp_path):
        path = tmp_path / "sec_fundamentals_daily.parquet"
        fresh = TODAY - datetime.timedelta(days=2)
        stale = TODAY - datetime.timedelta(days=90)
        pd.DataFrame({
            "date": [pd.Timestamp(fresh)] * 2 + [pd.Timestamp(stale)] * 3,
            "ticker": WATCHLIST,
        }).to_parquet(path)
        ctx = _ctx(config_extra=_contract(
            {"fundamentals_serving_axis": {"path": str(path)}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "fundamentals_serving_axis")
        assert axis["n_have"] == 2
        assert axis["n_expected"] == 5
        assert axis["coverage"] == pytest.approx(0.4)
        assert axis["verdict"] == AXIS_VIOLATION   # 0.4 < min_coverage 0.8

    def test_missing_feed_fires_dataset_missing(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "nope.parquet")}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "fundamentals_serving_axis")
        assert axis["present"] is False
        assert any("dataset_missing" in v for v in axis["violations"])


class TestAncientModelVintage:
    """The 2026-06-26 class: model trained to 2024-11 serving silently."""

    def _cfg(self, tmp_path, **artifact_kw) -> dict:
        artifact = _panel_artifact(tmp_path, **artifact_kw)
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}}
        # Declared (day-one contract-scope rule) so the axis is evaluated —
        # empty entry keeps the built-in defaults + degrade policy.
        cfg.update(_contract({"panel_model_artifact": {}}))
        return cfg

    def test_ancient_vintage_fires_degrade_by_default(self, tmp_path):
        cfg = self._cfg(tmp_path, trained="2025-01-15", cutoff="2024-11-30")
        ctx = _ctx(config_extra=cfg)
        assert DataAvailabilityGateTask().run(ctx) is True   # NOT darked
        axis = _axis(ctx, "panel_model_artifact")
        assert axis["verdict"] == AXIS_VIOLATION
        assert axis["policy"] == POLICY_DEGRADE
        assert any("train_vintage_stale" in v for v in axis["violations"])
        assert any("train_cutoff_stale" in v for v in axis["violations"])
        assert axis["as_of"] == "2024-11-30"

    def test_fail_closed_policy_blocks_buys_not_the_run(self, tmp_path):
        """Codex review (PR #187): fail_closed no longer raises from run().

        It records blocked=True; enforce_buy_block() (called AFTER the
        sell/exit pass in the real pipeline) is what actually gates buys.
        """
        cfg = self._cfg(tmp_path, trained="2025-01-15", cutoff="2024-11-30")
        cfg.update(_contract(
            {"panel_model_artifact": {"policy": "fail_closed"}}))
        ctx = _ctx(config_extra=cfg)
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True   # never raises
        assert _block(ctx)["verdict"] == VERDICT_BLOCKED
        assert _block(ctx)["blocked"] is True
        assert ctx.buy_blocked is False   # NOT applied yet — run() only records
        assert task.enforce_buy_block(ctx) is True
        assert ctx.buy_blocked is True
        assert ctx.counters["data_availability_buy_blocked"] == 1

    def test_missing_artifact_file_fires(self, tmp_path):
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(tmp_path / "gone.json"),
        }}}
        cfg.update(_contract({"panel_model_artifact": {}}))
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "panel_model_artifact")
        assert axis["present"] is False
        assert any("artifact_missing" in v for v in axis["violations"])

    def test_unstamped_provenance_is_not_a_pass(self, tmp_path):
        artifact = tmp_path / "panel-ltr.json"
        artifact.write_text(json.dumps({"kind": "panel_ltr_xgboost"}))
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}}
        cfg.update(_contract({"panel_model_artifact": {}}))
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "panel_model_artifact")
        assert any("trained_date_unstamped" in v for v in axis["violations"])
        assert any("train_cutoff_unstamped" in v for v in axis["violations"])

    def test_fingerprint_stamped_resolves(self, tmp_path):
        cfg = self._cfg(tmp_path)   # fresh dates + stamped sha
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "panel_model_artifact")
        assert axis["verdict"] == AXIS_OK
        assert axis["evidence"]["fingerprint_source"] == (
            "stamped:model_content_sha256")

    def test_panel_scoring_disabled_skips(self):
        cfg = {"ranking": {"panel_scoring": {"enabled": False}}}
        cfg.update(_contract({"panel_model_artifact": {}}))
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        assert _axis(ctx, "panel_model_artifact")["verdict"] == AXIS_SKIP


class TestWholeDatasetAbsence:
    """The SGOV class: nothing checked that a required dataset EXISTS."""

    def test_missing_dataset_fires(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"sleeve_sgov_bars": {
            "kind": "dataset_file",
            "path": str(tmp_path / "sleeve" / "SGOV.parquet"),
        }}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "sleeve_sgov_bars")
        assert axis["present"] is False
        assert any("dataset_missing" in v for v in axis["violations"])
        assert _block(ctx)["verdict"] == VERDICT_DEGRADED

    def test_present_dataset_with_fresh_vintage_ok(self, tmp_path):
        path = tmp_path / "SGOV.parquet"
        pd.DataFrame({"date": [pd.Timestamp(TODAY)]}).to_parquet(path)
        ctx = _ctx(config_extra=_contract({"sleeve_sgov_bars": {
            "kind": "dataset_file", "path": str(path),
            "date_column": "date", "max_staleness_days": 5,
        }}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "sleeve_sgov_bars")
        assert axis["verdict"] == AXIS_OK
        assert axis["as_of"] == TODAY.isoformat()

    def test_stale_dataset_vintage_fires(self, tmp_path):
        path = tmp_path / "SGOV.parquet"
        old = TODAY - datetime.timedelta(days=30)
        pd.DataFrame({"date": [pd.Timestamp(old)]}).to_parquet(path)
        ctx = _ctx(config_extra=_contract({"sleeve_sgov_bars": {
            "kind": "dataset_file", "path": str(path),
            "date_column": "date", "max_staleness_days": 5,
        }}))
        DataAvailabilityGateTask().run(ctx)
        assert any(
            "dataset_stale" in v
            for v in _axis(ctx, "sleeve_sgov_bars")["violations"]
        )

    def test_sealed_manifest_fingerprint_checked(self, tmp_path):
        data = tmp_path / "bars.parquet"
        pd.DataFrame({"date": [pd.Timestamp(TODAY)]}).to_parquet(data)
        manifest = tmp_path / "ingestion_manifest.json"
        manifest.write_text(json.dumps({"dataset_id": "x"}))   # NO fingerprint
        ctx = _ctx(config_extra=_contract({"crypto_bars": {
            "kind": "dataset_file", "path": str(data),
            "manifest": str(manifest),
        }}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "crypto_bars")
        assert any(
            "manifest_fingerprint_unresolvable" in v
            for v in axis["violations"]
        )
        manifest.write_text(json.dumps({"fingerprint": "sha256:abc"}))
        ctx2 = _ctx(config_extra=_contract({"crypto_bars": {
            "kind": "dataset_file", "path": str(data),
            "manifest": str(manifest),
        }}))
        DataAvailabilityGateTask().run(ctx2)
        assert _axis(ctx2, "crypto_bars")["verdict"] == AXIS_OK

    def test_invalid_custom_contract_fires(self):
        ctx = _ctx(config_extra=_contract({"mystery": {"kind": "wat"}}))
        DataAvailabilityGateTask().run(ctx)
        assert any(
            "contract_invalid" in v
            for v in _axis(ctx, "mystery")["violations"]
        )


class TestAdmissionCoverageCollapse:
    """The 07-08/09 class: 133/145 admission models stale → 0-ticker scan."""

    def test_collapse_fires(self):
        # Only 1/5 of the watchlist admitted+fresh → coverage 0.2 < 0.5.
        ctx = _ctx(models=_fresh_models(["AAA"]),
                   config_extra=_contract({"admission_model_metadata": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["verdict"] == AXIS_VIOLATION
        assert axis["coverage"] == pytest.approx(0.2)
        assert any(
            "admission_coverage_collapse" in v for v in axis["violations"])
        assert axis["evidence"]["verdict_counts"]["not_admitted"] == 4

    def test_stale_metadata_counts_against_coverage(self):
        # Admitted but stale-by-cutoff (the incident's exact mechanism —
        # reuses job_universe._classify_cutoffs, not a re-implementation).
        stale = _fresh_models(WATCHLIST, cutoff="2026-03-01")   # 131d > 60d
        ctx = _ctx(models=stale,
                   config_extra=_contract({"admission_model_metadata": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["coverage"] == 0.0
        assert axis["evidence"]["verdict_counts"]["stale"] == 5
        assert axis["verdict"] == AXIS_VIOLATION

    def test_healthy_universe_passes(self):
        ctx = _ctx(config_extra=_contract({"admission_model_metadata": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["verdict"] == AXIS_OK
        assert axis["coverage"] == 1.0


class TestOhlcvBars:
    def test_missing_symbol_and_coverage_fire(self):
        ohlcv = _fresh_ohlcv(WATCHLIST[:-1] + ["SPY"])   # EEE absent
        ctx = _ctx(ohlcv=ohlcv, config_extra=_contract({"ohlcv_bars": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_VIOLATION
        assert any("bars_missing:1/6" in v for v in axis["violations"])
        assert any("coverage" in v for v in axis["violations"])
        assert "EEE" in axis["evidence"]["missing_sample"]

    def test_stale_symbol_fires(self):
        ohlcv = _fresh_ohlcv(WATCHLIST + ["SPY"])
        ohlcv["AAA"] = _bars(TODAY - datetime.timedelta(days=10))
        ctx = _ctx(ohlcv=ohlcv, config_extra=_contract({"ohlcv_bars": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert any("bars_stale" in v for v in axis["violations"])
        assert axis["as_of"] == (TODAY - datetime.timedelta(days=10)).isoformat()

    def test_fresh_universe_passes(self):
        ctx = _ctx(config_extra=_contract({"ohlcv_bars": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_OK
        assert axis["coverage"] == 1.0
        assert axis["n_expected"] == 6   # watchlist + SPY


class TestCalibrator:
    def test_required_but_missing_fires(self, tmp_path):
        artifact = _panel_artifact(tmp_path)   # no calibration block
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
            "global_calibration": {"required": True},
        }}}
        cfg.update(_contract({"calibrator": {}}))
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "calibrator")
        assert axis["verdict"] == AXIS_VIOLATION
        assert any(
            "missing_global_calibration" in v for v in axis["violations"])

    def test_unconfigured_skips(self):
        ctx = _ctx(config_extra=_contract({"calibrator": {}}))
        DataAvailabilityGateTask().run(ctx)
        assert _axis(ctx, "calibrator")["verdict"] == AXIS_SKIP

    def test_valid_calibration_ok_and_stamp_presence_reported(self, tmp_path):
        artifact = _panel_artifact(tmp_path, calibration={
            "method": "linear", "slope": 1.2, "intercept": -0.1,
            "required": True, "model_content_sha256": "ab" * 32,
        })
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}}
        cfg.update(_contract({"calibrator": {}}))
        ctx = _ctx(config_extra=cfg)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "calibrator")
        assert axis["verdict"] == AXIS_OK
        assert axis["evidence"]["fingerprint_stamped"] is True


class TestRegimeAndAccountAxes:
    def test_benchmark_missing_fires(self):
        ctx = _ctx(ohlcv=_fresh_ohlcv(WATCHLIST),   # no SPY
                   config_extra=_contract({"regime_inputs": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "regime_inputs")
        assert any(
            "benchmark_bars_missing:SPY" in v for v in axis["violations"])

    def test_account_snapshot_absent_fires(self):
        ctx = _ctx(portfolio_value=0.0, cash=0.0,
                   config_extra=_contract({"account_snapshot": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "account_snapshot")
        assert any(
            "account_snapshot_absent" in v for v in axis["violations"])

    def test_account_snapshot_stale_stamp_fires(self):
        ctx = _ctx(
            account_snapshot_at=pd.Timestamp("2026-07-08 10:00:00"),
            run_timestamp=datetime.datetime(2026, 7, 10, 10, 0, 0),
            config_extra=_contract({"account_snapshot": {}}),
        )
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "account_snapshot")
        assert any(
            "account_snapshot_stale" in v for v in axis["violations"])

    def test_missing_stamp_is_evidence_not_violation(self):
        ctx = _ctx(config_extra=_contract({"account_snapshot": {}}))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "account_snapshot")
        assert axis["verdict"] == AXIS_OK
        assert axis["evidence"]["as_of_stamp"] == "unavailable"


# ── Contracts: declared, not hardcoded ────────────────────────────────────────

class TestContracts:
    def test_missing_contract_warns_loudly(self, caplog):
        ctx = _ctx()   # no data_contracts section at all
        with caplog.at_level("WARNING"):
            DataAvailabilityGateTask().run(ctx)
        block = _block(ctx)
        assert set(block["missing_contracts"]) == set(BUILTIN_CHECKERS)
        assert any(
            "NO DATA CONTRACT" in rec.message for rec in caplog.records)
        for name in BUILTIN_CHECKERS:
            assert block["axes"][name]["contract_declared"] is False

    def test_missing_contract_axis_is_unverified_not_evaluated(self):
        """Codex review (PR #187): day-one contract-scope rule.

        An axis with no reviewed contract entry gets NO freshness verdict
        (neither pass nor fail) — it is recorded as unverified and can never
        alarm or block, even under conditions (here: a coverage collapse)
        that WOULD have fired a violation had a contract been declared.
        """
        # Coverage collapse condition (would fire admission_coverage_collapse
        # if the axis were evaluated) — but NO data_contracts section at all.
        ctx = _ctx(models=_fresh_models(["AAA"]))
        DataAvailabilityGateTask().run(ctx)
        block = _block(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["verdict"] == AXIS_UNVERIFIED
        assert axis["violations"] == []
        assert axis["policy"] == POLICY_DEGRADE
        assert axis["contract_declared"] is False
        assert "admission_model_metadata" in block["missing_contracts"]
        assert "admission_model_metadata" not in block["axes_evaluated"]
        # Never contributes to fired/degraded/blocked.
        assert block["fired"] == []
        assert block["degraded"] is False
        assert block["verdict"] == VERDICT_AVAILABLE

    def test_malformed_contract_entry_is_also_unverified(self):
        """A non-mapping contract entry is treated the same as absent."""
        ctx = _ctx(config_extra=_contract({"admission_model_metadata": "oops"}),
                    models=_fresh_models(["AAA"]))
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["verdict"] == AXIS_UNVERIFIED
        assert "admission_model_metadata" in _block(ctx)["missing_contracts"]

    def test_fail_closed_on_missing_contract_axis_is_impossible(self, tmp_path):
        """An operator cannot declare fail_closed for an axis that has no
        contract at all — the axis stays unverified and inert regardless of
        how badly the underlying input is broken."""
        ctx = _ctx(models={})   # 0/5 coverage — would be a hard violation
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True
        assert _block(ctx)["blocked"] is False
        task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is False

    def test_declared_contract_suppresses_warning(self, tmp_path, caplog):
        ctx = _ctx(tmp_path, clean=True)
        with caplog.at_level("WARNING"):
            DataAvailabilityGateTask().run(ctx)
        assert _block(ctx)["missing_contracts"] == []
        assert not any(
            "NO DATA CONTRACT" in rec.message for rec in caplog.records)

    def test_contract_overrides_default_budget(self, tmp_path):
        fund = _fund_parquet(
            tmp_path, WATCHLIST, TODAY - datetime.timedelta(days=10))
        # 10d-old feed: violates a 5d budget, passes the default 20d.
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(fund), "max_staleness_days": 5}}))
        DataAvailabilityGateTask().run(ctx)
        assert _axis(
            ctx, "fundamentals_serving_axis")["verdict"] == AXIS_VIOLATION

    def test_default_policy_is_degrade_for_every_builtin_axis(self):
        for name in BUILTIN_CHECKERS:
            assert DEFAULT_CONTRACTS[name].get("policy", POLICY_DEGRADE) == (
                POLICY_DEGRADE), f"{name} must default to degrade_with_alarm"

    def test_unknown_policy_treated_as_degrade(self, tmp_path, caplog):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet"), "policy": "explode"}}))
        with caplog.at_level("WARNING"):
            assert DataAvailabilityGateTask().run(ctx) is True   # no abort
        assert _axis(
            ctx, "fundamentals_serving_axis")["policy"] == POLICY_DEGRADE

    def test_axis_disabled_via_contract_skips(self):
        ctx = _ctx(config_extra=_contract(
            {"account_snapshot": {"enabled": False}}),
            portfolio_value=0.0, cash=0.0)
        DataAvailabilityGateTask().run(ctx)
        assert _axis(ctx, "account_snapshot")["verdict"] == AXIS_SKIP


# ── Fail policy: fail_closed vs degrade_with_alarm ───────────────────────────

class TestFailPolicy:
    def test_degrade_lets_run_proceed_with_alarm(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet")}}))
        assert DataAvailabilityGateTask().run(ctx) is True
        block = _block(ctx)
        assert block["verdict"] == VERDICT_DEGRADED
        assert block["degraded"] is True and block["blocked"] is False
        assert ctx.counters["data_availability_degraded"] == 1
        assert ctx.counters["data_availability_blocked"] == 0
        fired = block["fired"]
        assert len(fired) == 1
        assert fired[0]["axis"] == "fundamentals_serving_axis"
        assert fired[0]["policy"] == POLICY_DEGRADE
        assert "dataset_missing" in fired[0]["reason"]

    def test_fail_closed_records_blocked_without_raising(self, tmp_path):
        """Codex review (PR #187): fail_closed no longer aborts run()."""
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet"),
            "policy": "fail_closed"}}))
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True   # never raises
        assert ctx.counters["data_availability_blocked"] == 1
        assert _axis(ctx, "fundamentals_serving_axis")["policy"] == (
            POLICY_FAIL_CLOSED)
        assert ctx.buy_blocked is False   # not enforced until enforce_buy_block
        assert task.enforce_buy_block(ctx) is True
        assert ctx.buy_blocked is True

    def test_mixed_policies_buy_block_names_only_fail_closed_axes(
            self, tmp_path, caplog):
        ctx = _ctx(
            models=_fresh_models(["AAA"]),        # degrade violation
            config_extra=_contract({
                "admission_model_metadata": {},    # declared → degrade (default)
                "fundamentals_serving_axis": {
                    "path": str(tmp_path / "gone.parquet"),
                    "policy": "fail_closed"},
            }),
        )
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True   # never raises
        block = _block(ctx)
        assert block["blocked"] is True
        assert block["degraded"] is True   # both axes fired
        caplog.clear()   # isolate enforce_buy_block's own message from run()'s
        with caplog.at_level("ERROR"):
            task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is True
        messages = "\n".join(rec.message for rec in caplog.records)
        assert "fundamentals_serving_axis" in messages
        assert "admission_model_metadata" not in messages


# ── Fail isolation ────────────────────────────────────────────────────────────

def _boom(_ctx_arg, _contract):
    raise ValueError("checker exploded")


class TestFailIsolation:
    def test_checker_crash_under_degrade_never_darks_the_run(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        # Must be DECLARED for the checker to run at all under the day-one
        # contract-scope rule — an undeclared axis is never even checked.
        ctx = _ctx(config_extra=_contract({"ohlcv_bars": {}}))
        assert DataAvailabilityGateTask(checkers=checkers).run(ctx) is True
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_ERROR
        assert "checker exploded" in axis["error"]
        assert _block(ctx)["verdict"] == VERDICT_DEGRADED

    def test_checker_crash_under_fail_closed_blocks_buys_not_the_run(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        ctx = _ctx(config_extra=_contract(
            {"ohlcv_bars": {"policy": "fail_closed"}}))
        task = DataAvailabilityGateTask(checkers=checkers)
        assert task.run(ctx) is True   # never raises
        assert _block(ctx)["blocked"] is True
        assert ctx.buy_blocked is False
        task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is True

    def test_whole_task_crash_swallowed_without_fail_closed(self, monkeypatch):
        task = DataAvailabilityGateTask()
        monkeypatch.setattr(
            task, "_build_block",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("assembly")),
        )
        ctx = _ctx()
        assert task.run(ctx) is True
        assert ctx.counters["data_availability_errors"] == 1
        block = _block(ctx)
        assert block["verdict"] is None
        assert block["blocked"] is False
        assert "assembly" in block["error"]
        task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is False

    def test_whole_task_crash_with_fail_closed_declared_blocks_buys_not_the_run(
            self, monkeypatch):
        """Codex review (PR #187): even the whole-task-crash path must never
        raise from run() — an unverifiable input under a declared fail_closed
        axis is recorded as blocked=True for enforce_buy_block(), not an
        abort of the pipeline (which would also dark the sell/exit pass)."""
        task = DataAvailabilityGateTask()
        monkeypatch.setattr(
            task, "_build_block",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("assembly")),
        )
        ctx = _ctx(config_extra=_contract(
            {"ohlcv_bars": {"policy": "fail_closed"}}))
        assert task.run(ctx) is True   # never raises
        block = _block(ctx)
        assert block["verdict"] == VERDICT_BLOCKED
        assert block["blocked"] is True
        assert ctx.counters["data_availability_blocked"] == 1
        assert ctx.buy_blocked is False   # not applied until enforce_buy_block
        task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is True

    def test_one_axis_crash_does_not_take_other_axes_dark(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        ctx = _ctx(config_extra=_contract({
            "ohlcv_bars": {}, "admission_model_metadata": {},
            "regime_inputs": {},
        }))
        DataAvailabilityGateTask(checkers=checkers).run(ctx)
        assert _axis(ctx, "admission_model_metadata")["verdict"] == AXIS_OK
        assert _axis(ctx, "regime_inputs")["verdict"] == AXIS_OK


# ── Behavior invariance + switches ────────────────────────────────────────────

class TestBehaviorInvariance:
    def test_degrade_violations_do_not_mutate_decision_state(self, tmp_path):
        ctx = _ctx(
            models=_fresh_models(["AAA"]),   # coverage collapse (degrade)
            config_extra=_contract({
                "admission_model_metadata": {},   # declared → real violation
                "fundamentals_serving_axis": {
                    "path": str(tmp_path / "gone.parquet")},
            }),
        )
        before = {
            "candidates": list(ctx.candidates),
            "exits": list(ctx.exits),
            "skip_buys": ctx.skip_buys,
            "buy_blocked": ctx.buy_blocked,
            "holdings": dict(ctx.holdings),
            "models": {k: dict(v) for k, v in ctx.models.items()},
        }
        task = DataAvailabilityGateTask()
        task.run(ctx)
        assert _block(ctx)["degraded"] is True   # sanity: a real violation fired
        assert _block(ctx)["blocked"] is False
        # enforce_buy_block is also a no-op here — nothing was fail_closed.
        task.enforce_buy_block(ctx)
        assert list(ctx.candidates) == before["candidates"]
        assert list(ctx.exits) == before["exits"]
        assert ctx.skip_buys == before["skip_buys"]
        assert ctx.buy_blocked == before["buy_blocked"]
        assert dict(ctx.holdings) == before["holdings"]
        assert {k: dict(v) for k, v in ctx.models.items()} == before["models"]

    def test_fail_closed_buy_block_mutates_only_buy_blocked(self, tmp_path):
        """The one intentional exception to behavior-invariance (module
        docstring): a fail_closed axis DOES mutate ctx.buy_blocked via
        enforce_buy_block — nothing else (never exits/candidates/holdings)."""
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet"),
            "policy": "fail_closed"}}))
        before = {
            "candidates": list(ctx.candidates),
            "exits": list(ctx.exits),
            "holdings": dict(ctx.holdings),
        }
        task = DataAvailabilityGateTask()
        task.run(ctx)
        task.enforce_buy_block(ctx)
        assert ctx.buy_blocked is True   # the one sanctioned mutation
        assert list(ctx.candidates) == before["candidates"]
        assert list(ctx.exits) == before["exits"]
        assert dict(ctx.holdings) == before["holdings"]

    def test_kill_switch(self):
        ctx = _ctx(config_extra={"data_availability": {"enabled": False}},
                   portfolio_value=0.0, cash=0.0)
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True
        assert getattr(ctx, CTX_ATTR, None) is None
        assert "data_availability_fired" not in ctx.counters
        assert task.enforce_buy_block(ctx) is True   # no-op, never raises
        assert ctx.buy_blocked is False

    def test_sell_only_skips(self):
        ctx = _ctx(portfolio_value=0.0, cash=0.0)
        ctx._run_mode = "sell-only"
        task = DataAvailabilityGateTask()
        assert task.run(ctx) is True
        assert getattr(ctx, CTX_ATTR, None) is None
        assert task.enforce_buy_block(ctx) is True   # no-op, never raises
        assert ctx.buy_blocked is False



# NOTE (Codex review, PR #187): no notification-formatting contract lives
# here anymore. This module publishes ctx.data_availability (the versioned,
# structured block) only; ntfy title/page rendering belongs to a separate
# orchestrator-repo consumer, not renquant-pipeline.


# ── pp_inference wiring ───────────────────────────────────────────────────────

class TestWiring:
    def test_runs_early_in_inference_pipeline_before_regime(self):
        from renquant_pipeline.kernel.pipeline import pp_inference
        source = inspect.getsource(pp_inference.InferencePipeline.run)
        gate = source.index("_data_availability_gate.run(ctx)")
        regime = source.index("RegimeJob().run(ctx)")
        freshness = source.index("DataFreshnessGateTask().run(ctx)")
        assert freshness < gate < regime, (
            "gate.run() must run EARLY: after the OHLCV freshness gate, "
            "before any decision logic (RegimeJob) — RECORD ONLY, never "
            "raises, never touches ctx.buy_blocked"
        )

    def test_enforce_buy_block_runs_after_the_sell_pass_before_buy_scan(self):
        """Codex review (PR #187) P1 fix: the buy-side block application
        must be wired strictly AFTER TickerSellJob (and its downstream
        exit-refinement tasks) and BEFORE the buy candidate scan — never
        before sells, so it can never suppress a risk-reducing exit."""
        from renquant_pipeline.kernel.pipeline import pp_inference
        source = inspect.getsource(pp_inference.InferencePipeline.run)
        gate_record = source.index("_data_availability_gate.run(ctx)")
        sell_pass = source.index("run_parallel(sell_tctxs, TickerSellJob())")
        short_cover = source.index("ShortCoverStopLossTask().run(ctx)")
        enforce = source.index("_data_availability_gate.enforce_buy_block(ctx)")
        buy_scan_cfg = source.index('score_db_cfg = ctx.config.get("score_db")')
        assert gate_record < sell_pass < short_cover < enforce < buy_scan_cfg, (
            "enforce_buy_block() must run after every sell/exit-evaluating "
            "task (TickerSellJob through ShortCoverStopLossTask) and before "
            "the Phase 2b buy candidate scan"
        )

    def test_not_wired_into_sell_only_pipeline(self):
        from renquant_pipeline.kernel.pipeline import pp_inference
        source = inspect.getsource(pp_inference.SellOnlyPipeline.run)
        assert "DataAvailabilityGateTask" not in source

    def test_axis_result_finalize_precedence(self):
        r = AxisResult("x", violations=["v"], error="e")
        assert r.finalize().verdict == AXIS_ERROR
        r2 = AxisResult("x", violations=["v"])
        assert r2.finalize().verdict == AXIS_VIOLATION
        r3 = AxisResult("x")
        assert r3.finalize().verdict == AXIS_OK


# ── Full-pipeline integration: fail_closed must never suppress a sell ────────

class TestFailClosedNeverSuppressesSells:
    """Codex review (PR #187) P1 — the whole point of the fix.

    Runs the REAL ``InferencePipeline().run(ctx)`` (not just the gate task in
    isolation) with a declared fail_closed data-availability violation, and
    proves a real stop-loss exit still fires for a held position. The buy
    universe is left empty (no models loaded) so the test never crosses the
    model-scoring boundary (xgboost / panel_scoring) — see
    tests/test_lift_pp_inference.py for why a full run() is otherwise
    avoided; TickerSellJob's path-rule exits (compute_exits) do not depend
    on a live model score (ScoreModelTask degrades to model_action="hold"
    when ctx.models[ticker] is None), so this is a safe, real exercise of
    the sell path.

    ``renquant_pipeline.kernel.meta_label.{task_meta_label_veto,
    job_meta_label_log}`` are not yet lifted into this repo (a separate,
    pre-existing gap — see test_lift_pp_inference.py's "NOT exercised here"
    note); ``InferencePipeline.run()`` imports them unconditionally, so a
    minimal fail-open stub is installed for the duration of this test only.
    The stub mirrors their documented no-op-by-default contract
    (meta_label veto/logging disabled unless explicitly opted in) and does
    not touch the control-flow ordering under test.
    """

    HELD = "ZZZ"

    @pytest.fixture(autouse=True)
    def _stub_unlifted_meta_label_modules(self, monkeypatch):
        veto_mod = types.ModuleType(
            "renquant_pipeline.kernel.meta_label.task_meta_label_veto")

        class _NoOpMetaLabelVetoTask:
            def run(self, ctx):
                return None

        veto_mod.MetaLabelVetoTask = _NoOpMetaLabelVetoTask

        log_mod = types.ModuleType(
            "renquant_pipeline.kernel.meta_label.job_meta_label_log")

        class _NoOpMetaLabelLoggingJob:
            def should_skip(self, ctx):
                return True   # matches "no-op in prod" documented default

            def run(self, ctx):
                return None

        log_mod.MetaLabelLoggingJob = _NoOpMetaLabelLoggingJob

        monkeypatch.setitem(
            sys.modules,
            "renquant_pipeline.kernel.meta_label.task_meta_label_veto",
            veto_mod,
        )
        monkeypatch.setitem(
            sys.modules,
            "renquant_pipeline.kernel.meta_label.job_meta_label_log",
            log_mod,
        )

    def _spy_bars(self, n: int = 260) -> pd.DataFrame:
        rng = np.random.default_rng(0)
        rets = rng.normal(0.0004, 0.007, n)   # calm bull — avoids BEAR carve-outs
        closes = 100.0 * np.cumprod(1.0 + rets)
        idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=n)
        return pd.DataFrame(
            {"open": closes, "high": closes * 1.001, "low": closes * 0.999,
             "close": closes, "volume": 1_000_000.0},
            index=idx,
        )

    def _held_bars(self, entry_price: float, current_price: float,
                    n: int = 30) -> pd.DataFrame:
        idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=n)
        closes = np.linspace(entry_price, current_price, n)
        return pd.DataFrame(
            {"open": closes, "high": closes * 1.001, "low": closes * 0.999,
             "close": closes, "volume": 100_000.0},
            index=idx,
        )

    def _full_ctx(self, tmp_path):
        from renquant_pipeline.kernel import regime as regime_mod

        entry_price = 100.0
        current_price = 88.0   # -12% — well past a 5% stop_loss_pct
        regime_params = {
            r: {"stop_loss_pct": 0.05}
            for r in ("BULL_CALM", "BULL_VOLATILE", "CHOPPY", "BEAR")
        }
        config = {
            "watchlist": [],           # empty buy universe on purpose
            "benchmark": "SPY",
            "regime_params": regime_params,
            "data_freshness": {"enabled": False},   # unrelated gate; not under test
            "data_contracts": {
                "schema": "data_contracts.v1",
                "axes": {
                    "fundamentals_serving_axis": {
                        "path": str(tmp_path / "nope.parquet"),  # missing → violation
                        "policy": "fail_closed",
                    },
                },
            },
        }
        ctx = InferenceContext(config=config, today=TODAY)
        ctx._run_mode = "full"
        ctx.regime_state = regime_mod.RegimeState()
        ctx.gmm = None
        ctx.spy_returns = list(
            pd.Series(self._spy_bars()["close"]).pct_change().dropna()
        )
        ctx.ohlcv = {
            "SPY": self._spy_bars(),
            self.HELD: self._held_bars(entry_price, current_price),
        }
        ctx.prices = {self.HELD: current_price}
        ctx.models = {}   # no models at all → empty buy universe, no scoring
        ctx.holdings = {
            self.HELD: HoldingState(
                entry_price=entry_price,
                entry_date=TODAY - datetime.timedelta(days=30),
                high_watermark=entry_price,
                shares=10.0,
            ),
        }
        ctx.portfolio_value = 10_000.0
        ctx.cash = 2_000.0
        return ctx

    def test_fail_closed_violation_still_emits_a_real_exit(self, tmp_path):
        from renquant_pipeline.kernel.pipeline.pp_inference import InferencePipeline

        ctx = self._full_ctx(tmp_path)
        InferencePipeline().run(ctx)   # must not raise

        # The data-availability gate DID record a fail-closed block…
        block = _block(ctx)
        assert block["blocked"] is True
        assert block["verdict"] == VERDICT_BLOCKED
        # …which gated NEW BUYS (the intended, sanctioned effect)…
        assert ctx.buy_blocked is True
        assert ctx.candidates == []   # empty buy universe either way
        # …but the held position's stop-loss exit STILL fired — the whole
        # point of the fix: a data-availability block must never suppress
        # a risk-reducing sell/exit decision.
        assert len(ctx.exits) == 1
        ticker, signal = ctx.exits[0]
        assert ticker == self.HELD
        assert signal.should_exit is True
        assert signal.exit_type == "stop_loss"
