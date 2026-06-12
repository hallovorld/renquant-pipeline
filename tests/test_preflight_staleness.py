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

    def test_xgb_kind_skips_for_now(self, tmp_path):
        ctx = PreflightContext(
            config={"ranking": {"panel_scoring": {"enabled": True,
                                                  "kind": "xgb",
                                                  "artifact_path": "x.json"}}},
            strategy_dir=tmp_path, broker=None, broker_name=None,
            run_mode="full")
        r = ModelStalenessTask().check(ctx)
        assert r.ok and "skip" in r.message
