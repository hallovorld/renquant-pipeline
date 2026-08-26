"""Tests for the governed WF-FAIL (passed=False) buy-admission override.

Covers the fail-closed validator (kernel.wf_fail_override), both enforcement
points (preflight P-WF-GATE passed=False branch + scoring-path admission), the
extra stringency vs diagnostic_only (wf_reason_acknowledged byte-equality), the
distinctness from diagnostic_only (a diagnostic_only authorization can NEVER
admit a passed=False artifact), behaviour invariance (with no authorization the
passed=False hard fail is byte-identical to today), and the config-fingerprint
invariant (the authorization key is OUTSIDE the model-relevant fingerprint
projection, so adding/expiring it never invalidates artifact config-consistency
stamps).
"""
from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _diagnostic_only_admission,
    _wf_fail_admission,
)
from renquant_pipeline.kernel.preflight_pipeline.ctx import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks.gate import (
    WfGateMetadataTask,
)
from renquant_pipeline.kernel.wf_fail_override import (
    evaluate_wf_fail_override,
    scorer_content_sha_from_payload,
)

TODAY = datetime.date(2026, 8, 10)
SCORER_SHA = "sha256:" + "ab" * 32

# A real passed=False wf_reason string (from a live renquant_104 gate stamp).
WF_REASON = (
    "FAIL: absolute_ok=True, benchmark_ok=False, regime_ok=False; "
    "mean Sharpe +0.602, 3/3 cuts > 0; SPY mean Sharpe +1.081, "
    "ΔSharpe -0.479, beat SPY Sharpe 1/3, beat SPY APY 0/3; "
    "benchmark-lag regimes=['HIGH_CALM', 'LOW_SPIKED']"
)


def _authorization(**overrides) -> dict:
    block = {
        "authorized": True,
        "operator": "renhao",
        "authorized_at": "2026-08-10",
        "expires": "2026-08-24",
        "scorer_model_content_sha256": SCORER_SHA,
        "wf_reason_acknowledged": WF_REASON,
        "reason": "08-10 directive: accept the WF-FAIL risk while the "
                  "benchmark repair is in flight",
    }
    block.update(overrides)
    return block


def _config(block: dict | None) -> dict:
    if block is None:
        return {}
    return {"wf_gate": {"wf_fail_buy_admission": block}}


def _failed_wf(**overrides) -> dict:
    wf = {"passed": False, "wf_reason": WF_REASON}
    wf.update(overrides)
    return wf


class TestValidatorFailClosed:

    def test_absent_block_is_silently_refused(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), {}, scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason == "absent"

    def test_non_dict_block_refused(self):
        v = evaluate_wf_fail_override(
            _failed_wf(),
            {"wf_gate": {"wf_fail_buy_admission": True}},
            scorer_content_sha=SCORER_SHA, now=TODAY)
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
        ("wf_reason_acknowledged", _authorization(wf_reason_acknowledged="")),
        ("wf_reason_acknowledged",
         _authorization(wf_reason_acknowledged="   ")),
        ("reason", _authorization(reason="  ")),
    ])
    def test_each_malformed_field_fails_closed(self, defect, block):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(block),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason.startswith("malformed:")
        assert defect in v.reason

    def test_unparseable_dates_fail_closed(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization(expires="soon")),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.reason == "malformed:expires"
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization(authorized_at="not-a-date")),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.reason == "malformed:authorized_at"

    def test_expired_is_hard_stop(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization(expires="2026-08-09")),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason == "expired"

    def test_expiry_date_itself_still_valid(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization(expires="2026-08-10")),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is True

    def test_wrong_scorer_hash_fails_closed(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization()),
            scorer_content_sha="sha256:" + "cd" * 32, now=TODAY)
        assert v.authorized is False
        assert v.reason == "scorer_mismatch"

    def test_no_scorer_identity_fails_closed(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization()),
            scorer_content_sha=None, now=TODAY)
        assert v.authorized is False
        assert v.reason == "scorer_hash_unavailable"

    def test_wf_reason_mismatch_fails_closed(self):
        # A stale authorization written for a DIFFERENT failure (ΔSharpe moved).
        stale = WF_REASON.replace("-0.479", "-0.545")
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization(wf_reason_acknowledged=stale)),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason == "wf_reason_mismatch"
        assert v.provenance["acknowledged"] == stale
        assert v.provenance["actual"] == WF_REASON

    def test_wf_reason_absent_on_artifact_fails_closed(self):
        v = evaluate_wf_fail_override(
            {"passed": False}, _config(_authorization()),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason == "wf_reason_unavailable"

    def test_happy_path_carries_full_provenance(self):
        v = evaluate_wf_fail_override(
            _failed_wf(), _config(_authorization()),
            scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is True
        assert v.reason == "authorized"
        assert v.provenance["operator"] == "renhao"
        assert v.provenance["expires"] == "2026-08-24"
        assert v.provenance["scorer_model_content_sha256"] == SCORER_SHA
        assert v.provenance["active_scorer_content_sha256"] == SCORER_SHA
        assert v.provenance["wf_reason_acknowledged"] == WF_REASON
        assert v.provenance["reason"]

    def test_payload_hash_helper_matches_renquant_common(self):
        common = pytest.importorskip("renquant_common.model_fingerprint")
        payload = {
            "kind": "panel_ltr",
            "feature_cols": ["a", "b"],
            "booster_raw_json": "{}",
            "trained_date": "2026-08-04",
            "params": {"objective": "rank:pairwise"},
        }
        expected = common.model_content_sha256(payload)
        assert scorer_content_sha_from_payload(payload) == expected
        v = evaluate_wf_fail_override(
            _failed_wf(),
            _config(_authorization(scorer_model_content_sha256=expected)),
            scorer_content_sha=scorer_content_sha_from_payload(payload),
            now=TODAY)
        assert v.authorized is True

    def test_payload_hash_helper_quiet_none(self):
        assert scorer_content_sha_from_payload(None) is None
        assert scorer_content_sha_from_payload({}) is None


class TestDistinctFromDiagnosticOnly:
    """A diagnostic_only authorization must NEVER admit a passed=False artifact."""

    def test_diagnostic_only_block_does_not_satisfy_wf_fail_override(self):
        cfg = {"wf_gate": {"diagnostic_only_buy_admission": {
            "authorized": True, "operator": "renhao",
            "authorized_at": "2026-08-10", "expires": "2026-08-24",
            "scorer_model_content_sha256": SCORER_SHA,
            "reason": "diagnostic-only authorization",
        }}}
        v = evaluate_wf_fail_override(
            _failed_wf(), cfg, scorer_content_sha=SCORER_SHA, now=TODAY)
        assert v.authorized is False
        assert v.reason == "absent"  # wrong key: wf_fail block is absent

    def test_wf_fail_block_does_not_satisfy_diagnostic_only_override(self):
        from renquant_pipeline.kernel.diagnostic_only_override import (
            evaluate_diagnostic_only_override,
        )
        v = evaluate_diagnostic_only_override(
            _config(_authorization()),
            scorer_v1_fingerprint=SCORER_SHA, today=TODAY)
        assert v.authorized is False
        assert v.reason == "absent"  # wrong key: diagnostic_only block is absent

    def test_preflight_diagnostic_only_auth_cannot_admit_passed_false(self, tmp_path: Path):
        # A diagnostic_only authorization present; artifact is passed=False.
        # P-WF-GATE consults diagnostic_only ONLY on the passed=True branch, so
        # the passed=False artifact still hard-fails.
        cfg = {"wf_gate": {"diagnostic_only_buy_admission": _authorization()}}
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(
            _failed_wf(), ctx, payload=None)
        assert result.severity == "hard"
        assert result.ok is False
        assert "wf_fail_override" not in result.details


class TestPreflightGateIntegration:

    def _payload(self):
        common = pytest.importorskip("renquant_common.model_fingerprint")
        payload = {
            "kind": "panel_ltr", "feature_cols": ["a"],
            "booster_raw_json": "{}", "params": {},
        }
        return payload, common.model_content_sha256(payload)

    def test_no_authorization_keeps_hard_block_byte_identical(self, tmp_path: Path):
        # BEHAVIOUR INVARIANCE: passed=False with no wf_fail_buy_admission block
        # hard-fails with the exact same reason/details as before this change.
        ctx = PreflightContext(config={}, strategy_dir=tmp_path, run_mode="full")
        wf = {"passed": False, "wf_3cut_sharpe_mean": 0.6017,
              "spy_sharpe_mean": 1.0808, "wf_reason": "FAIL: benchmark_ok=False"}
        result = WfGateMetadataTask()._evaluate_wf(wf, ctx, payload=None)
        assert result.severity == "hard"
        assert result.ok is False
        assert "wf_fail_override" not in result.details
        assert "wf_fail_override_rejected" not in result.details
        assert result.message == (
            "active panel artifact carries failed WF gate evidence: "
            "wf_sharpe_mean=0.6017 spy_sharpe_mean=1.0808 "
            "reason=FAIL: benchmark_ok=False. Refusing new live decisions until "
            "a WF-passing artifact is promoted or buy mode is explicitly "
            "isolated to shadow/research."
        )
        assert result.details["freshness_fallback_rfc210_refused"]

    def test_rejected_authorization_names_reason_in_message(self, tmp_path: Path):
        payload, sha = self._payload()
        cfg = _config(_authorization(scorer_model_content_sha256=sha,
                                     expires="2020-01-01"))
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(
            _failed_wf(), ctx, payload=payload)
        assert result.ok is False and result.severity == "hard"
        assert "rejected: expired" in result.message
        assert result.details["wf_fail_override_rejected"]["reason"] == "expired"

    def test_valid_authorization_admits_buys_with_provenance(self, tmp_path: Path):
        payload, sha = self._payload()
        cfg = _config(_authorization(
            scorer_model_content_sha256=sha,
            expires=(datetime.datetime.now(datetime.timezone.utc).date()
                     + datetime.timedelta(days=14)).isoformat(),
        ))
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(
            _failed_wf(), ctx, payload=payload)
        assert result.severity == "hard"
        assert result.ok is True
        assert "operator=renhao" in result.message
        assert "I-accept-the-risk" in result.message
        assert result.details["wf_fail_override"]["active_scorer_content_sha256"] == sha
        assert result.details["wf_fail_override"]["wf_reason_acknowledged"] == WF_REASON

    def test_wrong_scorer_authorization_blocks(self, tmp_path: Path):
        payload, _sha = self._payload()
        # _evaluate_wf reads the real clock, so a pinned fixture expiry would
        # make this test assert "expired" once the calendar passes it (it did,
        # 2026-08-25). Keep the authorization alive so the ONLY rejection
        # cause is the wrong scorer hash — same pattern as the valid-path test.
        cfg = _config(_authorization(
            scorer_model_content_sha256="sha256:" + "ef" * 32,
            expires=(datetime.datetime.now(datetime.timezone.utc).date()
                     + datetime.timedelta(days=14)).isoformat(),
        ))
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path, run_mode="full")
        result = WfGateMetadataTask()._evaluate_wf(
            _failed_wf(), ctx, payload=payload)
        assert result.ok is False
        assert result.details["wf_fail_override_rejected"]["reason"] == "scorer_mismatch"

    def test_sell_only_run_unchanged(self, tmp_path: Path):
        # Sell-only path never reaches the override — buys aren't happening.
        cfg = _config(_authorization())
        ctx = PreflightContext(config=cfg, strategy_dir=tmp_path,
                               run_mode="sell_only")
        result = WfGateMetadataTask()._evaluate_wf(_failed_wf(), ctx, payload=None)
        assert result.severity == "soft" and result.ok is True
        assert "wf_fail_override" not in result.details


class TestScoringPathIntegration:

    def _metadata(self, **wf_overrides) -> dict:
        wf = {"passed": False, "wf_reason": WF_REASON}
        wf.update(wf_overrides)
        return {
            "wf_gate_metadata": wf,
            "model_content_fingerprint_v1_recompute": SCORER_SHA,
        }

    def test_passed_false_without_block_passes_through(self):
        # BEHAVIOUR INVARIANCE: no authorization -> pass through (the live RFC
        # #210-served passed=False book must not be newly blocked here).
        ok, reason, details = _wf_fail_admission(self._metadata(), {}, today=TODAY)
        assert ok is True
        assert reason == "ok"
        assert details == {}

    def test_passed_true_is_untouched(self):
        meta = self._metadata(passed=True)
        ok, reason, details = _wf_fail_admission(
            meta, _config(_authorization()), today=TODAY)
        assert ok is True and reason == "ok" and details == {}

    def test_valid_authorization_admits_with_provenance(self):
        ok, reason, details = _wf_fail_admission(
            self._metadata(), _config(_authorization()), today=TODAY)
        assert ok is True
        assert reason == "ok:wf_fail_operator_override"
        assert details["wf_fail_override"]["operator"] == "renhao"

    def test_wrong_scorer_authorization_blocks_with_rejection_detail(self):
        meta = self._metadata()
        meta["model_content_fingerprint_v1_recompute"] = "sha256:" + "ef" * 32
        ok, reason, details = _wf_fail_admission(
            meta, _config(_authorization()), today=TODAY)
        assert ok is False
        assert reason == "regime_admission:wf_fail_evidence"
        assert details["wf_fail_override_rejected"]["reason"] == "scorer_mismatch"

    def test_wf_reason_mismatch_blocks(self):
        stale = WF_REASON.replace("-0.479", "-0.545")
        ok, reason, details = _wf_fail_admission(
            self._metadata(), _config(_authorization(wf_reason_acknowledged=stale)),
            today=TODAY)
        assert ok is False
        assert details["wf_fail_override_rejected"]["reason"] == "wf_reason_mismatch"

    def test_expired_authorization_blocks(self):
        ok, reason, details = _wf_fail_admission(
            self._metadata(), _config(_authorization(expires="2020-01-01")),
            today=TODAY)
        assert ok is False
        assert details["wf_fail_override_rejected"]["reason"] == "expired"

    def test_diagnostic_only_admission_ignores_passed_false_non_diagnostic(self):
        # The sibling scoring guard only acts on diagnostic_only=True; a plain
        # passed=False is not its concern (proves the two guards are distinct).
        ok, reason, details = _diagnostic_only_admission(self._metadata(), {})
        assert ok is True and reason == "ok"


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
        with_override["wf_gate"] = {"wf_fail_buy_admission": _authorization()}
        assert cc.fingerprint_config(base) == cc.fingerprint_config(with_override)
        assert "wf_gate" not in cc._model_relevant_fields(with_override)
