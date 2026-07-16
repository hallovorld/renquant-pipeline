"""Tests for the governed diagnostic-only buy-admission override.

Covers the fail-closed validator (kernel.diagnostic_only_override), both
enforcement points (preflight P-WF-GATE + scoring-path admission), and the
config-fingerprint invariant (the authorization key is OUTSIDE the
model-relevant fingerprint projection, so adding/expiring it never
invalidates artifact config-consistency stamps).
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from renquant_pipeline.kernel.diagnostic_only_override import (
    evaluate_diagnostic_only_override,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _diagnostic_only_admission,
)
from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import (
    WfGateMetadataTask,
)

TODAY = datetime.date(2026, 7, 16)
SCORER_SHA = "sha256:" + "ab" * 32


def _authorization(**overrides) -> dict:
    block = {
        "authorized": True,
        "operator": "renhao",
        "authorized_at": "2026-07-16",
        "expires": "2026-08-15",
        "scorer_model_content_sha256": SCORER_SHA,
        "reason": "06-22 directive: run XGB while WF gate repair is in flight",
    }
    block.update(overrides)
    return block


def _config(block: dict | None) -> dict:
    if block is None:
        return {}
    return {"wf_gate": {"diagnostic_only_buy_admission": block}}


class TestValidatorFailClosed:

    def test_absent_block_is_silently_refused(self):
        v = evaluate_diagnostic_only_override(
            {}, scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is False
        assert v.reason == "absent"

    def test_non_dict_block_refused(self):
        v = evaluate_diagnostic_only_override(
            {"wf_gate": {"diagnostic_only_buy_admission": True}},
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is False
        assert v.reason == "malformed:not_a_dict"

    @pytest.mark.parametrize("defect,block", [
        ("authorized", _authorization(authorized="yes")),
        ("authorized", _authorization(authorized=1)),
        ("operator", _authorization(operator="")),
        ("authorized_at", _authorization(authorized_at=None)),
        ("expires", _authorization(expires="")),
        ("scorer_model_content_sha256",
         _authorization(scorer_model_content_sha256="")),
        ("reason", _authorization(reason="  ")),
    ])
    def test_each_malformed_field_fails_closed(self, defect, block):
        v = evaluate_diagnostic_only_override(
            _config(block), scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is False
        assert v.reason.startswith("malformed:")
        assert defect in v.reason

    def test_unparseable_dates_fail_closed(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization(expires="soon")),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.reason == "malformed:expires"
        v = evaluate_diagnostic_only_override(
            _config(_authorization(authorized_at="not-a-date")),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.reason == "malformed:authorized_at"

    def test_expired_is_hard_stop(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization(expires="2026-07-15")),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is False
        assert v.reason == "expired"

    def test_expiry_date_itself_still_valid(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization(expires="2026-07-16")),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is True

    def test_wrong_scorer_hash_fails_closed(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization()),
            scorer_v1_fingerprint="sha256:" + "cd" * 32, today=TODAY)
        assert v.authorized is False
        assert v.reason == "scorer_mismatch"

    def test_no_scorer_identity_fails_closed(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization()), today=TODAY)
        assert v.authorized is False
        assert v.reason == "scorer_hash_unavailable"

    def test_happy_path_carries_full_provenance(self):
        v = evaluate_diagnostic_only_override(
            _config(_authorization()),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is True
        assert v.provenance["operator"] == "renhao"
        assert v.provenance["expires"] == "2026-08-15"
        assert v.provenance["scorer_model_content_sha256"] == SCORER_SHA
        assert v.provenance["active_scorer_v1"] == SCORER_SHA
        assert v.provenance["reason"]

    def test_payload_hash_computed_via_renquant_common(self):
        common = pytest.importorskip("renquant_common.model_fingerprint")
        payload = {
            "kind": "panel_ltr",
            "feature_cols": ["a", "b"],
            "booster_raw_json": "{}",
            "trained_date": "2026-06-21",
            "params": {"objective": "rank:pairwise"},
        }
        expected = common.model_content_sha256(payload)
        v = evaluate_diagnostic_only_override(
            _config(_authorization(scorer_model_content_sha256=expected)),
            scorer_payload=payload, today=TODAY)
        assert v.authorized is True
        assert v.provenance["active_scorer_v1"] == expected


class TestPreflightGateIntegration:

    def _wf(self) -> dict:
        return {
            "passed": True,
            "diagnostic_only": True,
            "wf_3cut_sharpe_mean": 1.2,
            "wf_3cut_apy_mean": 0.2,
            "spy_sharpe_mean": 0.8,
            "strategy_minus_spy_sharpe_mean": 0.4,
            "n_cuts_beat_spy_sharpe": 3,
            "sanity_regime_ic": {"passed": True},
        }

    def test_no_authorization_keeps_hard_block(self, tmp_path: Path):
        ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(self._wf(), ctx)
        assert result.severity == "hard"
        assert result.ok is False

    def test_rejected_authorization_names_reason_in_message(self, tmp_path: Path):
        cfg = _config(_authorization(expires="2020-01-01"))
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(
            self._wf(), ctx, payload={"kind": "panel_ltr"})
        assert result.ok is False
        assert "rejected: expired" in result.message
        assert result.details["diagnostic_only_override_rejected"]["reason"] == "expired"

    def test_valid_authorization_admits_buys_with_provenance(self, tmp_path: Path):
        common = pytest.importorskip("renquant_common.model_fingerprint")
        payload = {
            "kind": "panel_ltr",
            "feature_cols": ["a"],
            "booster_raw_json": "{}",
            "params": {},
        }
        sha = common.model_content_sha256(payload)
        cfg = _config(_authorization(
            scorer_model_content_sha256=sha,
            expires=(datetime.datetime.now(datetime.timezone.utc).date()
                     + datetime.timedelta(days=30)).isoformat(),
        ))
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(self._wf(), ctx, payload=payload)
        assert result.severity == "hard"
        assert result.ok is True
        assert "operator=renhao" in result.message
        assert result.details["diagnostic_only_override"]["active_scorer_v1"] == sha


class TestScoringPathIntegration:

    def _metadata(self) -> dict:
        return {
            "wf_gate_metadata": {"diagnostic_only": True, "passed": True},
            "model_content_fingerprint_v1_recompute": SCORER_SHA,
        }

    def test_no_authorization_still_blocks(self):
        ok, reason, details = _diagnostic_only_admission(self._metadata(), {})
        assert ok is False
        assert reason == "regime_admission:diagnostic_only_wf_evidence"
        assert "diagnostic_only_override_rejected" not in details

    def test_valid_authorization_admits_with_provenance(self):
        ok, reason, details = _diagnostic_only_admission(
            self._metadata(), _config(_authorization()), today=TODAY)
        assert ok is True
        assert reason == "ok:diagnostic_only_operator_override"
        assert details["diagnostic_only_override"]["operator"] == "renhao"

    def test_wrong_scorer_authorization_blocks_with_rejection_detail(self):
        meta = self._metadata()
        meta["model_content_fingerprint_v1_recompute"] = "sha256:" + "ef" * 32
        ok, reason, details = _diagnostic_only_admission(
            meta, _config(_authorization()), today=TODAY)
        assert ok is False
        assert details["diagnostic_only_override_rejected"]["reason"] == "scorer_mismatch"

    def test_missing_runtime_fingerprint_blocks(self):
        meta = {"wf_gate_metadata": {"diagnostic_only": True, "passed": True}}
        ok, reason, _ = _diagnostic_only_admission(
            meta, _config(_authorization()), today=TODAY)
        assert ok is False


class TestConfigFingerprintUnaffected:

    def test_authorization_key_outside_fingerprint_projection(self):
        cc = pytest.importorskip("renquant_common.config_consistency")
        base = {
            "watchlist": ["AAPL", "MSFT", "SPY"],
            "benchmark": "SPY",
            "sector_map": {"AAPL": "giant_tech", "MSFT": "giant_tech"},
            "sector_etf_map": {"giant_tech": "XLK"},
            "panel_ltr": {"lookahead_days": 60,
                          "xgb_params": {"objective": "rank:pairwise"}},
        }
        with_override = dict(base)
        with_override["wf_gate"] = {
            "diagnostic_only_buy_admission": _authorization(),
        }
        assert cc.fingerprint_config(base) == cc.fingerprint_config(with_override)
        assert "wf_gate" not in cc._model_relevant_fields(with_override)
