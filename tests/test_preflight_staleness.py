"""P-MODEL-STALENESS tests (eng plan §0.5 retrain rails + decay curve)."""
from __future__ import annotations

import datetime as dt
import json

from renquant_pipeline.kernel.preflight_pipeline import (
    ModelStalenessTask,
    PreflightContext,
)


def _write_artifact(tmp_path, *, trained_days_ago, cutoff_days_ago):
    model = tmp_path / "hf_patchtst_all_seed44_model.pt"
    model.write_bytes(b"pt")
    today = dt.date.today()
    meta = {
        "trained_date": (today - dt.timedelta(days=trained_days_ago)).isoformat(),
        "effective_train_cutoff_date":
            (today - dt.timedelta(days=cutoff_days_ago)).isoformat(),
    }
    (tmp_path / "hf_patchtst_all_seed44_model.pt.metadata.json").write_text(
        json.dumps(meta))
    return "hf_patchtst_all_seed44_model.pt"


def _ctx(tmp_path, rel, **staleness_cfg) -> PreflightContext:
    config = {
        "ranking": {"panel_scoring": {"enabled": True, "kind": "hf_patchtst",
                                      "artifact_path": rel}},
    }
    if staleness_cfg:
        config["preflight"] = {"staleness": staleness_cfg}
    return PreflightContext(config=config, strategy_dir=tmp_path,
                            broker=None, broker_name=None, run_mode="full")


class TestStaleness:

    def test_fresh_model_passes(self, tmp_path):
        rel = _write_artifact(tmp_path, trained_days_ago=20, cutoff_days_ago=200)
        r = ModelStalenessTask().check(_ctx(tmp_path, rel))
        assert r.ok and r.severity == "soft"
        assert r.details["retrain_age_days"] == 20

    def test_retrain_rail_breach_warns(self, tmp_path):
        rel = _write_artifact(tmp_path, trained_days_ago=150, cutoff_days_ago=200)
        r = ModelStalenessTask().check(_ctx(tmp_path, rel))
        assert not r.ok and r.severity == "soft"
        assert "quarterly rail" in r.message

    def test_cutoff_decay_breach_warns(self, tmp_path):
        rel = _write_artifact(tmp_path, trained_days_ago=20, cutoff_days_ago=400)
        r = ModelStalenessTask().check(_ctx(tmp_path, rel))
        assert not r.ok
        assert "decay-curve" in r.message

    def test_config_knobs_override_defaults(self, tmp_path):
        rel = _write_artifact(tmp_path, trained_days_ago=150, cutoff_days_ago=200)
        r = ModelStalenessTask().check(
            _ctx(tmp_path, rel, max_retrain_age_days=365))
        assert r.ok

    def test_missing_dates_is_soft_fail_not_pass(self, tmp_path):
        model = tmp_path / "hf_patchtst_all_seed44_model.pt"
        model.write_bytes(b"pt")
        (tmp_path / "hf_patchtst_all_seed44_model.pt.metadata.json").write_text(
            '{"training_contract": {"seed": 44}}')  # content, but no dates
        r = ModelStalenessTask().check(
            _ctx(tmp_path, "hf_patchtst_all_seed44_model.pt"))
        assert not r.ok and r.severity == "soft"
        assert "provenance gap" in r.message

    def test_disabled_panel_skips(self, tmp_path):
        ctx = PreflightContext(
            config={"ranking": {"panel_scoring": {"enabled": False}}},
            strategy_dir=tmp_path, broker=None, broker_name=None,
            run_mode="full")
        r = ModelStalenessTask().check(ctx)
        assert r.ok and "skip" in r.message

    # ── xgb primary now covered (2026-06-27): the live primary is xgb; the
    # check must read its trained_date instead of skipping ──────────────────

    def _write_xgb(self, tmp_path, *, trained_days_ago, cutoff_days_ago=None):
        today = dt.date.today()
        meta = {"kind": "panel_ltr_xgboost",
                "trained_date": (today - dt.timedelta(days=trained_days_ago)).isoformat()}
        if cutoff_days_ago is not None:
            meta["effective_train_cutoff_date"] = (
                today - dt.timedelta(days=cutoff_days_ago)).isoformat()
        (tmp_path / "panel-ltr.alpha158_fund.json").write_text(json.dumps(meta))
        return "panel-ltr.alpha158_fund.json"

    def _xgb_ctx(self, tmp_path, rel):
        return PreflightContext(
            config={"ranking": {"panel_scoring": {"enabled": True, "kind": "xgb",
                                                  "artifact_path": rel}}},
            strategy_dir=tmp_path, broker=None, broker_name=None, run_mode="full")

    def test_xgb_primary_retrain_rail_is_now_evaluated(self, tmp_path):
        # fresh trained_date, no cutoff stamped → retrain rail OK but cutoff is a
        # surfaced provenance gap (no longer a silent skip-pass).
        rel = self._write_xgb(tmp_path, trained_days_ago=39)
        r = ModelStalenessTask().check(self._xgb_ctx(tmp_path, rel))
        assert r.details["retrain_age_days"] == 39      # it READ the xgb dates
        assert not r.ok and r.severity == "soft"
        assert "unstamped" in r.message and "skip" not in r.message

    def test_xgb_retrain_breach_warns(self, tmp_path):
        rel = self._write_xgb(tmp_path, trained_days_ago=150, cutoff_days_ago=200)
        r = ModelStalenessTask().check(self._xgb_ctx(tmp_path, rel))
        assert not r.ok and "quarterly rail" in r.message

    def test_xgb_fully_fresh_with_cutoff_passes(self, tmp_path):
        rel = self._write_xgb(tmp_path, trained_days_ago=20, cutoff_days_ago=200)
        r = ModelStalenessTask().check(self._xgb_ctx(tmp_path, rel))
        assert r.ok and r.severity == "soft"

    def test_xgb_missing_trained_date_is_soft_fail(self, tmp_path):
        (tmp_path / "panel-ltr.alpha158_fund.json").write_text('{"kind": "xgb"}')
        r = ModelStalenessTask().check(
            self._xgb_ctx(tmp_path, "panel-ltr.alpha158_fund.json"))
        assert not r.ok and "provenance gap" in r.message
