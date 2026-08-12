"""P-MODEL-STALENESS for kind == "blend": a z-composite is only as fresh as
its OLDEST leg.

Registered 2026-08-11. Before this, `kind='blend'` took the unrecognised-kind
else branch, so the LIVE prod z-blend scorer's decay/retrain rail was never
established and the daily model_freshness monitor reported "binding data cutoff
unknown / kind not registered". `ModelStalenessTask._check_blend` now resolves
EACH leg the SAME way the per-kind branches resolve a solo scorer (direct-
artifact JSON leg read like xgb; hf_patchtst leg read via the sequence
sidecar), binds the blend's freshness to the STALEST leg, and fail-closes —
SOFT, naming the leg — on any leg whose kind/date this rail cannot establish.

The real prod shape (verified 2026-08-11 in strategy-104 strategy_config.json):
component 0 = production panel scorer JSON (no `kind` key -> the "panel"
default), component 1 = {"kind": "momentum_residual", ...} ledger pointer.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from renquant_pipeline.kernel.preflight_pipeline.tasks.staleness import (
    ModelStalenessTask,
)


class _Ctx:
    """Minimal PreflightContext stand-in (mirrors test_staleness_unknown_kind)."""

    def __init__(self, strategy_dir: Path, components, **staleness):
        self.strategy_dir = strategy_dir
        panel = {"enabled": True, "kind": "blend"}
        if components is not None:
            panel["components"] = components
            # top-level artifact_path is vestigial for a blend (freshness comes
            # from the legs) but present in the real config — mirror that.
            if components and isinstance(components[0], dict):
                panel["artifact_path"] = components[0].get("artifact_path")
        self.config = {"ranking": {"panel_scoring": panel}}
        if staleness:
            self.config["preflight"] = {"staleness": staleness}


def _iso(days_ago: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()


def _panel_leg(tmp_path: Path, name: str, *, trained_days_ago,
               cutoff_days_ago=None, kind=None) -> dict:
    """A direct-artifact leg: JSON with trained_date (+ optional cutoff).

    ``kind=None`` omits the key entirely -> the "panel" default (the exact
    shape of the prod production-scorer leg). ``kind="xgb"`` etc. exercises the
    explicit direct-artifact kinds.
    """
    payload = {"trained_date": _iso(trained_days_ago)}
    if cutoff_days_ago is not None:
        payload["effective_train_cutoff_date"] = _iso(cutoff_days_ago)
    (tmp_path / name).write_text(json.dumps(payload))
    entry = {"artifact_path": name}
    if kind is not None:
        entry["kind"] = kind
    return entry


def _patchtst_leg(tmp_path: Path, name: str, *, trained_days_ago,
                  cutoff_days_ago) -> dict:
    """An hf_patchtst leg: .pt + sequence sidecar (read via _load_sequence_sidecar)."""
    (tmp_path / name).write_bytes(b"pt")
    meta = {"trained_date": _iso(trained_days_ago),
            "effective_train_cutoff_date": _iso(cutoff_days_ago)}
    (tmp_path / (name + ".metadata.json")).write_text(json.dumps(meta))
    return {"kind": "hf_patchtst", "artifact_path": name}


# ── the core requirement: stalest leg binds, both ages reported, SOFT ────────
def test_two_legs_stalest_binds_and_reports_both_ages(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "clf.json", trained_days_ago=60, cutoff_days_ago=300),
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is True and r.severity == "soft"
    # binding = the OLDER leg (component 1) on both axes
    assert r.details["binding_retrain_leg"] == 1
    assert r.details["binding_cutoff_leg"] == 1
    assert "component[1]" in r.message and "blend fresh" in r.message
    # BOTH legs' ages survive into details
    ages = {leg["index"]: leg["retrain_age_days"] for leg in r.details["legs"]}
    assert ages == {0: 20, 1: 60}
    cutoffs = {leg["index"]: leg["cutoff_age_days"] for leg in r.details["legs"]}
    assert cutoffs == {0: 200, 1: 300}


def test_stalest_leg_drives_the_breach(tmp_path):
    """The binding leg is what trips the rail — not the fresh one."""
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "slow.json", trained_days_ago=150, cutoff_days_ago=300),
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "quarterly rail" in r.message
    assert "component[1]" in r.message
    assert r.details["binding_retrain_leg"] == 1


def test_config_knob_overrides_apply_to_binding_leg(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "slow.json", trained_days_ago=150, cutoff_days_ago=300),
    ]
    r = ModelStalenessTask().check(
        _Ctx(tmp_path, legs, max_retrain_age_days=365))
    assert r.ok is True and r.severity == "soft"


# ── fail-closed: an unresolvable leg is a SURFACED gap, never a false pass ───
def test_momentum_residual_leg_is_soft_nonpass_naming_it(tmp_path):
    """The REAL prod shape: leg1 kind='momentum_residual' (ledger axis this rail
    does not register). It must fail closed NAMING the leg, even though the other
    leg is perfectly fresh — a blend is only as fresh as its oldest leg."""
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        {"kind": "momentum_residual",
         "artifact_path": "artifacts/momentum/momentum_artifact_ledger.jsonl"},
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"          # NOT a false pass
    assert "component[1]" in r.message
    assert "momentum_residual" in r.message
    # the fresh leg's age is still reported so the finding is actionable
    assert "component[0]" in r.message and "retrain_age=20d" in r.message


def test_leg_missing_trained_date_is_soft_nonpass_naming_it(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "undated.json", trained_days_ago=40),  # then strip it
    ]
    (tmp_path / "undated.json").write_text(json.dumps({"note": "no dates"}))
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "component[1]" in r.message and "trained_date" in r.message


def test_unreadable_leg_artifact_is_soft_nonpass_naming_it(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        {"artifact_path": "bad.json"},
    ]
    (tmp_path / "bad.json").write_text("{not json")
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "component[1]" in r.message and "unreadable" in r.message


def test_missing_artifact_path_leg_is_soft_nonpass(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        {"kind": "panel"},  # no artifact_path
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "component[1]" in r.message and "artifact_path missing" in r.message


def test_missing_components_is_soft_nonpass(tmp_path):
    r = ModelStalenessTask().check(_Ctx(tmp_path, None))
    assert r.ok is False and r.severity == "soft"
    assert "components is missing or empty" in r.message


def test_empty_components_is_soft_nonpass(tmp_path):
    r = ModelStalenessTask().check(_Ctx(tmp_path, []))
    assert r.ok is False and r.severity == "soft"
    assert "components is missing or empty" in r.message


# ── decay rail: an unstamped cutoff on ANY leg is a surfaced gap (like xgb) ──
def test_unstamped_cutoff_leg_surfaces_decay_gap_but_reads_retrain(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "nocut.json", trained_days_ago=30),  # no cutoff
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "unstamped" in r.message and "decay-curve" in r.message
    assert "component[1]" in r.message
    assert r.details["binding_cutoff_leg"] is None
    # retrain axis was still read for both legs
    assert r.details["binding_retrain_leg"] == 1


def test_cutoff_decay_breach_binds_to_oldest_cutoff(tmp_path):
    legs = [
        _panel_leg(tmp_path, "prod.json", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "old.json", trained_days_ago=30, cutoff_days_ago=400),
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is False and r.severity == "soft"
    assert "decay-curve knee" in r.message
    assert r.details["binding_cutoff_leg"] == 1


# ── leg-kind resolution is REUSED (hf_patchtst via the sidecar, xgb via JSON) ─
def test_hf_patchtst_leg_is_read_via_sidecar(tmp_path):
    legs = [
        _patchtst_leg(tmp_path, "seq_model.pt", trained_days_ago=20, cutoff_days_ago=200),
        _panel_leg(tmp_path, "clf.json", trained_days_ago=40, cutoff_days_ago=300, kind="xgb"),
    ]
    r = ModelStalenessTask().check(_Ctx(tmp_path, legs))
    assert r.ok is True and r.severity == "soft"
    assert r.details["binding_retrain_leg"] == 1  # panel/xgb leg is older
    kinds = {leg["index"]: leg["kind"] for leg in r.details["legs"]}
    assert kinds == {0: "hf_patchtst", 1: "xgb"}
