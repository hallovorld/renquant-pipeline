"""P-MODEL-STALENESS momentum ledger legs (orch#906).

Before this change the blend branch surfaced every ``momentum_residual`` leg
as "not a staleness-readable leg kind" — the LIVE prod z-blend's slow-momentum
leg could never establish a freshness axis. The reader maps the chain-verified
ledger TAIL ROW onto the rail's two axes: ``appended_at_utc`` (the weekly
publish stamp = retrain clock) and ``cutoff_date`` (the formation cutoff, the
serving contract's declared staleness surface).

Uses the REAL ledger-chain machinery from the renquant-model distribution
(importorskip, same convention as test_blend_momentum_component).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

mm = pytest.importorskip(
    "renquant_model_momentum",
    reason="renquant-model distribution not on path (sibling checkout)")

from renquant_pipeline.kernel.preflight_pipeline import (  # noqa: E402
    ModelStalenessTask,
    PreflightContext,
)
from renquant_pipeline.kernel.preflight_pipeline.tasks import staleness as st  # noqa: E402


def _append_row(ledger: Path, *, cutoff: str, params_version: str = "v0") -> dict:
    """Append one chain-valid row via the model package's ONE chain writer."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    return mm.append_chained_row({
        "kind": "momentum_residual_v0",
        "cutoff_date": cutoff,
        "effective_train_cutoff_date": cutoff,
        "cutoff_embargo_days": 21,
        "params_version": params_version,
        "artifact_content_sha256": "ab" * 32,
    }, ledger)


def _xgb_leg(tmp_path: Path, *, trained_days_ago=20, cutoff_days_ago=200) -> str:
    today = dt.date.today()
    meta = {
        "kind": "panel_ltr_xgboost",
        "trained_date": (today - dt.timedelta(days=trained_days_ago)).isoformat(),
        "effective_train_cutoff_date": (
            today - dt.timedelta(days=cutoff_days_ago)).isoformat(),
    }
    (tmp_path / "panel-ltr.json").write_text(json.dumps(meta))
    return "panel-ltr.json"


def _blend_ctx(tmp_path: Path, comps: list) -> PreflightContext:
    config = {"ranking": {"panel_scoring": {
        "enabled": True, "kind": "blend", "components": comps}}}
    return PreflightContext(config=config, strategy_dir=tmp_path,
                            broker=None, broker_name=None, run_mode="full")


def _solo_ctx(tmp_path: Path, rel: str) -> PreflightContext:
    config = {"ranking": {"panel_scoring": {
        "enabled": True, "kind": "momentum_residual", "artifact_path": rel}}}
    return PreflightContext(config=config, strategy_dir=tmp_path,
                            broker=None, broker_name=None, run_mode="full")


def test_leg_kind_literal_matches_the_canonical_constant():
    # The preflight module mirrors the scorer package's constant as a literal
    # to stay import-light; a drifted mirror is worse than no mirror.
    from renquant_pipeline.kernel.panel_pipeline.blend_scorer import (
        MOMENTUM_COMPONENT_KIND,
    )
    assert st.MOMENTUM_LEG_KIND == MOMENTUM_COMPONENT_KIND


class TestBlendMomentumLeg:

    def _comps(self, tmp_path):
        return [
            {"kind": "panel", "artifact_path": _xgb_leg(tmp_path)},
            {"kind": "momentum_residual",
             "artifact_path": "momentum/momentum_artifact_ledger.jsonl"},
        ]

    def test_momentum_leg_is_now_staleness_readable(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        fresh_cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        _append_row(ledger, cutoff=fresh_cutoff)
        r = ModelStalenessTask().check(_blend_ctx(tmp_path, self._comps(tmp_path)))
        assert "not a staleness-readable" not in r.message
        assert r.ok and r.severity == "soft"
        legs = r.details["legs"]
        assert legs[1]["kind"] == "momentum_residual"
        assert legs[1]["trained_date"] == dt.date.today().isoformat()  # appended today
        assert legs[1]["effective_train_cutoff_date"] == fresh_cutoff
        assert legs[1]["ledger_row_index"] == 0

    def test_stale_momentum_cutoff_binds_the_blend(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        stale_cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        _append_row(ledger, cutoff=stale_cutoff)
        r = ModelStalenessTask().check(_blend_ctx(tmp_path, self._comps(tmp_path)))
        assert not r.ok
        assert "decay-curve" in r.message
        assert "component[1]" in r.message  # the momentum leg is the binding leg

    def test_tampered_ledger_is_a_surfaced_gap_not_a_pass(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        c1 = (dt.date.today() - dt.timedelta(days=14)).isoformat()
        c2 = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        _append_row(ledger, cutoff=c1)
        _append_row(ledger, cutoff=c2)
        lines = ledger.read_text(encoding="utf-8").splitlines()
        row0 = json.loads(lines[0])
        row0["cutoff_date"] = "1999-01-01"  # rewrite history
        lines[0] = json.dumps(row0, sort_keys=True, separators=(",", ":"))
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
        r = ModelStalenessTask().check(_blend_ctx(tmp_path, self._comps(tmp_path)))
        assert not r.ok
        assert "component[1]" in r.message  # named, fail-closed

    def test_empty_ledger_is_a_surfaced_gap(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text("", encoding="utf-8")
        r = ModelStalenessTask().check(_blend_ctx(tmp_path, self._comps(tmp_path)))
        assert not r.ok
        assert "PENDING_FIRST_ARTIFACT" in r.message

    def test_missing_ledger_is_a_surfaced_gap(self, tmp_path):
        r = ModelStalenessTask().check(_blend_ctx(tmp_path, self._comps(tmp_path)))
        assert not r.ok
        assert "component[1]" in r.message


class TestSoloMomentum:

    def test_fresh_solo_momentum_passes_the_rails(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        fresh_cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        _append_row(ledger, cutoff=fresh_cutoff)
        r = ModelStalenessTask().check(
            _solo_ctx(tmp_path, "momentum/momentum_artifact_ledger.jsonl"))
        assert r.ok and r.severity == "soft"
        assert r.details["retrain_age_days"] == 0
        assert "not a registered scoring kind" not in r.message

    def test_stale_solo_momentum_cutoff_warns(self, tmp_path):
        ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
        stale_cutoff = (dt.date.today() - dt.timedelta(days=400)).isoformat()
        _append_row(ledger, cutoff=stale_cutoff)
        r = ModelStalenessTask().check(
            _solo_ctx(tmp_path, "momentum/momentum_artifact_ledger.jsonl"))
        assert not r.ok
        assert "decay-curve" in r.message
