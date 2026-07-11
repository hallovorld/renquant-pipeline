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

* contracts DECLARED, not hardcoded — a consumed axis with no contract is
  checked with defaults AND loudly warned (missing_contracts);
* per-axis fail policy: fail_closed aborts loudly; degrade_with_alarm (the
  day-one default for EVERY axis) proceeds with the alarm in
  ctx.data_availability + counters;
* clean pass → verdict AVAILABLE, nothing fired, nothing raised;
* fail isolation: a checker crash under degrade never darks the run; under
  fail_closed it blocks (an unverifiable input is a fail, not a pass); a
  whole-task crash is swallowed unless a fail_closed axis is declared;
* ZERO behavior change to decision state (regression pin);
* kill switch + sell-only skip;
* the notification contract (#463 universe_health stamping pattern);
* pp_inference wiring (early in InferencePipeline only, before RegimeJob,
  never in SellOnlyPipeline).
"""
from __future__ import annotations

import datetime
import inspect
import json

import pandas as pd
import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.pipeline.task_data_availability import (
    AXIS_ERROR,
    AXIS_OK,
    AXIS_SKIP,
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
    notification_fields,
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
        return {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}}

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

    def test_fail_closed_policy_aborts_loudly(self, tmp_path):
        cfg = self._cfg(tmp_path, trained="2025-01-15", cutoff="2024-11-30")
        cfg.update(_contract(
            {"panel_model_artifact": {"policy": "fail_closed"}}))
        ctx = _ctx(config_extra=cfg)
        with pytest.raises(RuntimeError, match="INPUT UNAVAILABLE"):
            DataAvailabilityGateTask().run(ctx)
        # The block is stamped BEFORE the abort so the bundle still sees it.
        assert _block(ctx)["verdict"] == VERDICT_BLOCKED
        assert _block(ctx)["blocked"] is True

    def test_missing_artifact_file_fires(self, tmp_path):
        cfg = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(tmp_path / "gone.json"),
        }}}
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
        ctx = _ctx(config_extra={"ranking": {"panel_scoring": {
            "enabled": False}}})
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
        ctx = _ctx(models=_fresh_models(["AAA"]))
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
        ctx = _ctx(models=stale)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["coverage"] == 0.0
        assert axis["evidence"]["verdict_counts"]["stale"] == 5
        assert axis["verdict"] == AXIS_VIOLATION

    def test_healthy_universe_passes(self):
        ctx = _ctx()
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "admission_model_metadata")
        assert axis["verdict"] == AXIS_OK
        assert axis["coverage"] == 1.0


class TestOhlcvBars:
    def test_missing_symbol_and_coverage_fire(self):
        ohlcv = _fresh_ohlcv(WATCHLIST[:-1] + ["SPY"])   # EEE absent
        ctx = _ctx(ohlcv=ohlcv)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_VIOLATION
        assert any("bars_missing:1/6" in v for v in axis["violations"])
        assert any("coverage" in v for v in axis["violations"])
        assert "EEE" in axis["evidence"]["missing_sample"]

    def test_stale_symbol_fires(self):
        ohlcv = _fresh_ohlcv(WATCHLIST + ["SPY"])
        ohlcv["AAA"] = _bars(TODAY - datetime.timedelta(days=10))
        ctx = _ctx(ohlcv=ohlcv)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert any("bars_stale" in v for v in axis["violations"])
        assert axis["as_of"] == (TODAY - datetime.timedelta(days=10)).isoformat()

    def test_fresh_universe_passes(self):
        ctx = _ctx()
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_OK
        assert axis["coverage"] == 1.0
        assert axis["n_expected"] == 6   # watchlist + SPY


class TestCalibrator:
    def test_required_but_missing_fires(self, tmp_path):
        artifact = _panel_artifact(tmp_path)   # no calibration block
        ctx = _ctx(config_extra={"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
            "global_calibration": {"required": True},
        }}})
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "calibrator")
        assert axis["verdict"] == AXIS_VIOLATION
        assert any(
            "missing_global_calibration" in v for v in axis["violations"])

    def test_unconfigured_skips(self):
        ctx = _ctx()
        DataAvailabilityGateTask().run(ctx)
        assert _axis(ctx, "calibrator")["verdict"] == AXIS_SKIP

    def test_valid_calibration_ok_and_stamp_presence_reported(self, tmp_path):
        artifact = _panel_artifact(tmp_path, calibration={
            "method": "linear", "slope": 1.2, "intercept": -0.1,
            "required": True, "model_content_sha256": "ab" * 32,
        })
        ctx = _ctx(config_extra={"ranking": {"panel_scoring": {
            "enabled": True, "kind": "panel_ltr_xgboost",
            "artifact_path": str(artifact),
        }}})
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "calibrator")
        assert axis["verdict"] == AXIS_OK
        assert axis["evidence"]["fingerprint_stamped"] is True


class TestRegimeAndAccountAxes:
    def test_benchmark_missing_fires(self):
        ctx = _ctx(ohlcv=_fresh_ohlcv(WATCHLIST))   # no SPY
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "regime_inputs")
        assert any(
            "benchmark_bars_missing:SPY" in v for v in axis["violations"])

    def test_account_snapshot_absent_fires(self):
        ctx = _ctx(portfolio_value=0.0, cash=0.0)
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "account_snapshot")
        assert any(
            "account_snapshot_absent" in v for v in axis["violations"])

    def test_account_snapshot_stale_stamp_fires(self):
        ctx = _ctx(
            account_snapshot_at=pd.Timestamp("2026-07-08 10:00:00"),
            run_timestamp=datetime.datetime(2026, 7, 10, 10, 0, 0),
        )
        DataAvailabilityGateTask().run(ctx)
        axis = _axis(ctx, "account_snapshot")
        assert any(
            "account_snapshot_stale" in v for v in axis["violations"])

    def test_missing_stamp_is_evidence_not_violation(self):
        ctx = _ctx()
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

    def test_fail_closed_aborts(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet"),
            "policy": "fail_closed"}}))
        with pytest.raises(RuntimeError, match="fundamentals_serving_axis"):
            DataAvailabilityGateTask().run(ctx)
        assert ctx.counters["data_availability_blocked"] == 1
        assert _axis(ctx, "fundamentals_serving_axis")["policy"] == (
            POLICY_FAIL_CLOSED)

    def test_mixed_policies_abort_names_only_fail_closed_axes(self, tmp_path):
        ctx = _ctx(
            models=_fresh_models(["AAA"]),        # degrade violation
            config_extra=_contract({"fundamentals_serving_axis": {
                "path": str(tmp_path / "gone.parquet"),
                "policy": "fail_closed"}}),
        )
        with pytest.raises(RuntimeError) as excinfo:
            DataAvailabilityGateTask().run(ctx)
        assert "fundamentals_serving_axis" in str(excinfo.value)
        assert "admission_model_metadata" not in str(excinfo.value)


# ── Fail isolation ────────────────────────────────────────────────────────────

def _boom(_ctx_arg, _contract):
    raise ValueError("checker exploded")


class TestFailIsolation:
    def test_checker_crash_under_degrade_never_darks_the_run(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        ctx = _ctx()
        assert DataAvailabilityGateTask(checkers=checkers).run(ctx) is True
        axis = _axis(ctx, "ohlcv_bars")
        assert axis["verdict"] == AXIS_ERROR
        assert "checker exploded" in axis["error"]
        assert _block(ctx)["verdict"] == VERDICT_DEGRADED

    def test_checker_crash_under_fail_closed_blocks(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        ctx = _ctx(config_extra=_contract(
            {"ohlcv_bars": {"policy": "fail_closed"}}))
        with pytest.raises(RuntimeError, match="ohlcv_bars"):
            DataAvailabilityGateTask(checkers=checkers).run(ctx)

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
        assert "assembly" in block["error"]

    def test_whole_task_crash_with_fail_closed_declared_raises(
            self, monkeypatch):
        task = DataAvailabilityGateTask()
        monkeypatch.setattr(
            task, "_build_block",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("assembly")),
        )
        ctx = _ctx(config_extra=_contract(
            {"ohlcv_bars": {"policy": "fail_closed"}}))
        with pytest.raises(RuntimeError, match="refusing to proceed"):
            task.run(ctx)

    def test_one_axis_crash_does_not_take_other_axes_dark(self):
        checkers = dict(BUILTIN_CHECKERS)
        checkers["ohlcv_bars"] = _boom
        ctx = _ctx()
        DataAvailabilityGateTask(checkers=checkers).run(ctx)
        assert _axis(ctx, "admission_model_metadata")["verdict"] == AXIS_OK
        assert _axis(ctx, "regime_inputs")["verdict"] == AXIS_OK


# ── Behavior invariance + switches ────────────────────────────────────────────

class TestBehaviorInvariance:
    def test_degrade_violations_do_not_mutate_decision_state(self, tmp_path):
        ctx = _ctx(
            models=_fresh_models(["AAA"]),   # coverage collapse (degrade)
            config_extra=_contract({"fundamentals_serving_axis": {
                "path": str(tmp_path / "gone.parquet")}}),
        )
        before = {
            "candidates": list(ctx.candidates),
            "exits": list(ctx.exits),
            "skip_buys": ctx.skip_buys,
            "buy_blocked": ctx.buy_blocked,
            "holdings": dict(ctx.holdings),
            "models": {k: dict(v) for k, v in ctx.models.items()},
        }
        DataAvailabilityGateTask().run(ctx)
        assert list(ctx.candidates) == before["candidates"]
        assert list(ctx.exits) == before["exits"]
        assert ctx.skip_buys == before["skip_buys"]
        assert ctx.buy_blocked == before["buy_blocked"]
        assert dict(ctx.holdings) == before["holdings"]
        assert {k: dict(v) for k, v in ctx.models.items()} == before["models"]

    def test_kill_switch(self):
        ctx = _ctx(config_extra={"data_availability": {"enabled": False}},
                   portfolio_value=0.0, cash=0.0)
        assert DataAvailabilityGateTask().run(ctx) is True
        assert getattr(ctx, CTX_ATTR, None) is None
        assert "data_availability_fired" not in ctx.counters

    def test_sell_only_skips(self):
        ctx = _ctx(portfolio_value=0.0, cash=0.0)
        ctx._run_mode = "sell-only"
        assert DataAvailabilityGateTask().run(ctx) is True
        assert getattr(ctx, CTX_ATTR, None) is None


# ── Notification contract (#463 universe_health stamping pattern) ────────────

class TestNotificationFields:
    def test_not_evaluated(self):
        fields = notification_fields(None)
        assert fields["degraded"] is False
        assert fields["blocked"] is False
        assert fields["title_tag"] == "UNKNOWN"

    def test_degraded(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet")}}))
        DataAvailabilityGateTask().run(ctx)
        fields = notification_fields(_block(ctx))
        assert fields["degraded"] is True
        assert fields["blocked"] is False
        assert fields["title_tag"] == "DATA-DEGRADED"
        assert "fundamentals_serving_axis" in fields["line"]

    def test_clean(self, tmp_path):
        ctx = _ctx(tmp_path, clean=True)
        DataAvailabilityGateTask().run(ctx)
        fields = notification_fields(_block(ctx))
        assert fields["title_tag"] == "DATA-OK"
        assert fields["degraded"] is False

    def test_blocked(self, tmp_path):
        ctx = _ctx(config_extra=_contract({"fundamentals_serving_axis": {
            "path": str(tmp_path / "gone.parquet"),
            "policy": "fail_closed"}}))
        with pytest.raises(RuntimeError):
            DataAvailabilityGateTask().run(ctx)
        fields = notification_fields(_block(ctx))
        assert fields["blocked"] is True
        assert fields["title_tag"] == "DATA-BLOCKED"


# ── pp_inference wiring ───────────────────────────────────────────────────────

class TestWiring:
    def test_runs_early_in_inference_pipeline_before_regime(self):
        from renquant_pipeline.kernel.pipeline import pp_inference
        source = inspect.getsource(pp_inference.InferencePipeline.run)
        gate = source.index("DataAvailabilityGateTask().run(ctx)")
        regime = source.index("RegimeJob().run(ctx)")
        freshness = source.index("DataFreshnessGateTask().run(ctx)")
        assert freshness < gate < regime, (
            "gate must run EARLY: after the OHLCV freshness gate, before any "
            "decision logic (RegimeJob)"
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
