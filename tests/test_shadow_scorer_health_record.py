"""Shadow-scorer HEALTH RECORD — canonical silent-failure contract.

The shadow panel scorer (PatchTST) is fail-soft: a broken ``../../``
artifact_path makes it load-fail and CONTINUE, so a G4-critical comparison feed
can die for weeks with nothing but a per-run ``log.warning``. These tests pin
the canonical ``shadow_health`` contract that ``ApplyShadowScoringTask`` emits
and that the CI gate (#525) + sentinel (#566) reuse:

  * artifact IDENTITY (immutable content digest + provenance), not mere path
    existence — a swapped file changes the digest;
  * EXPECTED-SKIP vs FAULT — a by-design non-run (disabled / no models / no
    candidates) is ``actionable=True``; a real setup/degradation problem is
    ``actionable=False`` with reason tokens; a record is emitted BEFORE every
    early return so the sentinel never sees ambiguous silence;
  * the pure resolve+identity+verdict helpers, unit-tested directly.
"""
from __future__ import annotations

import datetime
import json
import os
from types import SimpleNamespace

import pandas as pd
import pytest

import renquant_pipeline.kernel.panel_pipeline.shadow_health as sh
import renquant_pipeline.kernel.panel_pipeline.shadow_scoring as shadow_scoring
from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
    SHADOW_HEALTH_SCHEMA,
    STATE_DEGRADED,
    STATE_DISABLED,
    STATE_LOAD_FAILED,
    STATE_NO_CANDIDATES,
    STATE_NO_SHADOW_MODELS,
    STATE_NOT_SCORED,
    STATE_OK,
    STATE_UNRESOLVED_ARTIFACT,
    STATUS_EXPECTED_SKIP,
    STATUS_FAULT,
    STATUS_OK,
    content_digest,
    finalize_shadow_health,
    mark_expected_skip,
    new_shadow_health,
    resolve_artifact_identity,
)
from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import ApplyShadowScoringTask

IDX = ["AAA", "BBB", "CCC"]
RUN_DATE = datetime.date(2026, 7, 21)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Isolate tests — the module-level scorer + digest caches leak across runs."""
    shadow_scoring._SCORER_CACHE.clear()
    sh._DIGEST_CACHE.clear()
    yield
    shadow_scoring._SCORER_CACHE.clear()
    sh._DIGEST_CACHE.clear()


# ── content_digest: immutable identity ─────────────────────────────────────────

def test_content_digest_none_for_missing(tmp_path):
    assert content_digest(tmp_path / "nope.pt") is None
    assert content_digest(None) is None
    assert content_digest(tmp_path) is None   # a directory is not a file


def test_content_digest_changes_when_bytes_change(tmp_path):
    art = tmp_path / "model.pt"
    art.write_bytes(b"one")
    d1 = content_digest(art)
    assert d1.startswith("sha256:")
    # rewrite with different bytes + bump mtime so the (path,mtime,size) cache busts
    art.write_bytes(b"two-different")
    os.utime(art, (art.stat().st_atime, art.stat().st_mtime + 5))
    sh._DIGEST_CACHE.clear()
    d2 = content_digest(art)
    assert d2 != d1   # a swapped file → a different immutable identity


# ── resolve_artifact_identity: canonical resolution + source ───────────────────

def test_resolve_identity_strategy_dir_source(tmp_path):
    art = tmp_path / "artifacts" / "model.pt"
    art.parent.mkdir(parents=True)
    art.write_bytes(b"m")
    ident = resolve_artifact_identity("artifacts/model.pt", strategy_dir=tmp_path)
    assert ident.resolved is True
    assert ident.source == "strategy_dir"
    assert ident.content_sha256 == content_digest(art)


def test_resolve_identity_repo_root_source(tmp_path):
    # Standard layout: strategy_dir = <root>/backtesting/renquant_104 → repo_root
    # is two levels up; an artifact present only under repo_root resolves there.
    strategy_dir = tmp_path / "backtesting" / "renquant_104"
    strategy_dir.mkdir(parents=True)
    art = tmp_path / "sub" / "model.pt"
    art.parent.mkdir(parents=True)
    art.write_bytes(b"m")
    ident = resolve_artifact_identity("sub/model.pt", strategy_dir=strategy_dir)
    assert ident.resolved is True
    assert ident.source == "repo_root"


def test_resolve_identity_unresolved_names_error(tmp_path):
    ident = resolve_artifact_identity("../../broken/model.pt", strategy_dir=tmp_path)
    assert ident.resolved is False
    assert ident.source == "unresolved"
    assert ident.content_sha256 is None
    assert ident.error


def test_resolve_identity_no_strategy_dir(tmp_path):
    ident = resolve_artifact_identity("model.pt", strategy_dir=None)
    assert ident.resolved is False
    assert ident.source == "unresolved"


# ── Pure verdict logic (finalize_shadow_health) ────────────────────────────────

def _loaded_health(**over):
    h = new_shadow_health(
        shadow_name="patchtst_v1", kind="hf_patchtst",
        artifact_path="artifacts/prod/model.pt", run_date=RUN_DATE,
        run_id="run-1", n_candidates=3,
    )
    h["loaded"] = True
    h["artifact_resolved"] = True
    h["content_sha256"] = "sha256:deadbeefdeadbeef"
    h["config_fingerprint"] = "cfg-abc"
    h["effective_train_cutoff_date"] = "2026-07-10"   # 11d → fresh
    h["n_scored"] = 3
    h["coverage_frac"] = 1.0
    h.update(over)
    return h


def test_finalize_ok_when_fresh_covered_and_identified():
    h = finalize_shadow_health(_loaded_health(), run_date=RUN_DATE)
    assert h["state"] == STATE_OK
    assert h["status"] == STATUS_OK
    assert h["actionable"] is True
    assert h["reasons"] == []
    assert h["staleness_days"] == 11


def test_finalize_stale_is_degraded_fault():
    h = finalize_shadow_health(
        _loaded_health(effective_train_cutoff_date="2026-01-01"), run_date=RUN_DATE)
    assert h["state"] == STATE_DEGRADED
    assert h["status"] == STATUS_FAULT
    assert h["actionable"] is False
    assert any(r.startswith("stale_") for r in h["reasons"])


def test_finalize_low_coverage_is_degraded_fault():
    h = finalize_shadow_health(
        _loaded_health(n_scored=1, coverage_frac=1 / 3), run_date=RUN_DATE)
    assert h["state"] == STATE_DEGRADED
    assert any(r.startswith("low_coverage_") for r in h["reasons"])


def test_finalize_missing_required_identity():
    h = finalize_shadow_health(
        _loaded_health(content_sha256=None, config_fingerprint=None,
                       effective_train_cutoff_date=None),
        run_date=RUN_DATE)
    assert h["status"] == STATUS_FAULT
    assert "missing_content_sha256" in h["reasons"]
    assert "missing_config_fingerprint" in h["reasons"]
    assert "missing_train_cutoff" in h["reasons"]


def test_finalize_pinned_identity_mismatch():
    h = finalize_shadow_health(
        _loaded_health(expected_content_sha256="sha256:0000000000000000",
                       expected_config_fingerprint="cfg-OTHER"),
        run_date=RUN_DATE)
    assert h["status"] == STATUS_FAULT
    assert "content_sha256_mismatch" in h["reasons"]
    assert "config_fingerprint_mismatch" in h["reasons"]


def test_finalize_pinned_identity_match_is_ok():
    h = finalize_shadow_health(
        _loaded_health(expected_content_sha256="sha256:deadbeefdeadbeef",
                       expected_config_fingerprint="cfg-abc"),
        run_date=RUN_DATE)
    assert h["actionable"] is True
    assert h["reasons"] == []


def test_finalize_future_cutoff():
    h = finalize_shadow_health(
        _loaded_health(effective_train_cutoff_date="2026-09-01"), run_date=RUN_DATE)
    assert any(r.startswith("train_cutoff_future_") for r in h["reasons"])


def test_finalize_not_loaded_unresolved_vs_load_failed():
    unresolved = new_shadow_health(
        shadow_name="s", kind="hf_patchtst", artifact_path="../../bad.pt",
        run_date=RUN_DATE, run_id="r", n_candidates=3)
    unresolved["artifact_resolved"] = False
    finalize_shadow_health(unresolved, run_date=RUN_DATE)
    assert unresolved["state"] == STATE_UNRESOLVED_ARTIFACT
    assert unresolved["status"] == STATUS_FAULT
    assert unresolved["actionable"] is False
    assert unresolved["reasons"] == ["artifact_unresolved"]

    load_failed = new_shadow_health(
        shadow_name="s", kind="hf_patchtst", artifact_path="ok.pt",
        run_date=RUN_DATE, run_id="r", n_candidates=3)
    load_failed["artifact_resolved"] = True   # file exists but loader raised
    finalize_shadow_health(load_failed, run_date=RUN_DATE)
    assert load_failed["state"] == STATE_LOAD_FAILED
    assert load_failed["reasons"] == ["load_failed"]


def test_finalize_zero_scores_is_not_scored():
    h = _loaded_health(n_scored=0, coverage_frac=0.0,
                       skip_reason="degenerate_cross_section")
    finalize_shadow_health(h, run_date=RUN_DATE)
    assert h["state"] == STATE_NOT_SCORED
    assert "degenerate_cross_section" in h["reasons"]


# ── Expected-skip semantics (loaded=false + actionable=TRUE) ───────────────────

@pytest.mark.parametrize("state", [STATE_DISABLED, STATE_NO_SHADOW_MODELS, STATE_NO_CANDIDATES])
def test_mark_expected_skip_is_actionable_true(state):
    h = new_shadow_health(shadow_name=None, kind=None, artifact_path=None,
                          run_date=RUN_DATE, run_id="r", n_candidates=0)
    mark_expected_skip(h, state, "because")
    assert h["loaded"] is False
    assert h["actionable"] is True          # expected, NOT a fault
    assert h["status"] == STATUS_EXPECTED_SKIP
    assert h["state"] == state
    # finalize must pass an expected-skip record through unchanged
    finalize_shadow_health(h, run_date=RUN_DATE)
    assert h["actionable"] is True
    assert h["status"] == STATUS_EXPECTED_SKIP


def test_mark_expected_skip_rejects_fault_state():
    h = new_shadow_health(shadow_name=None, kind=None, artifact_path=None,
                          run_date=RUN_DATE, run_id="r", n_candidates=0)
    with pytest.raises(ValueError):
        mark_expected_skip(h, STATE_DEGRADED, "nope")


# ── Task-level integration (writes the JSONL sink) ─────────────────────────────

class _RecordingXGB:
    requires_history = False
    feature_cols = ["KMID", "KLEN"]

    def __init__(self, metadata, scores):
        self.metadata = metadata
        self._scores = scores

    def score(self, X):
        return pd.Series({t: self._scores.get(t, float("nan"))
                          for t in X.index}, dtype=float)


class _LoaderHandler:
    def __init__(self, scorer):
        self._scorer = scorer

    def scorer_loader(self, p, cfg):
        return self._scorer


class _FailingHandler:
    def scorer_loader(self, p, cfg):
        raise FileNotFoundError(f"cannot open artifact: {p}")


_VARIED = pd.DataFrame({"KMID": [0.1, 0.5, 0.9], "KLEN": [1.0, 2.0, 3.0]}, index=IDX)
_FRESH_META = {"effective_train_cutoff_date": "2026-07-10", "config_fingerprint": "cfg-1"}
_FULL_SCORES = {"AAA": 3.0, "BBB": 2.0, "CCC": 1.0}


def _ctx(tmp_path, *, shadow_models, candidates=None, matrix=None,
         shadow_enabled=True, shadow_health=None):
    if candidates is None:
        candidates = [SimpleNamespace(ticker=t, panel_score=float(i + 1), rank_score=None)
                      for i, t in enumerate(IDX)]
    panel = {
        "shadow_models": shadow_models,
        "shadow_log_mlflow": False,
        "kind": "hf_patchtst",
    }
    if shadow_enabled is False:
        panel["shadow_enabled"] = False
    cfg = {"ranking": {"panel_scoring": panel}, "_strategy_dir": str(tmp_path)}
    if shadow_health is not None:
        cfg["shadow_health"] = shadow_health
    return SimpleNamespace(
        config=cfg, candidates=candidates,
        _panel_matrix=_VARIED if matrix is None else matrix,
        today=RUN_DATE, run_id="run-xyz", holdings={}, regime="BULL_CALM",
        counters={},
    )


def _read_records(tmp_path):
    sink = tmp_path / "logs" / "shadow_scorer_health.jsonl"
    assert sink.exists(), "health JSONL sink was not written"
    return [json.loads(line) for line in sink.read_text().splitlines() if line]


def _wire_loader(monkeypatch, tmp_path, scorer):
    """Real artifact file at tmp_path/model.pt + a handler returning ``scorer``."""
    art = tmp_path / "model.pt"
    art.write_bytes(b"artifact-bytes")
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: art)
    monkeypatch.setattr(registry, "get", lambda kind: _LoaderHandler(scorer))
    return art


def test_run_emits_ok_record_for_healthy_shadow(monkeypatch, tmp_path):
    art = _wire_loader(monkeypatch, tmp_path,
                       _RecordingXGB(dict(_FRESH_META), dict(_FULL_SCORES)))
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                                  "artifact_path": "model.pt"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["schema"] == SHADOW_HEALTH_SCHEMA
    assert rec["shadow_name"] == "patchtst_v1"
    assert rec["run_id"] == "run-xyz"
    assert rec["loaded"] is True
    assert rec["artifact_resolved"] is True
    assert rec["content_sha256"] == content_digest(art)   # immutable identity captured
    assert rec["config_fingerprint"] == "cfg-1"
    assert rec["staleness_days"] == 11
    assert rec["n_scored"] == 3
    assert rec["coverage_frac"] == pytest.approx(1.0)
    assert rec["state"] == STATE_OK
    assert rec["status"] == STATUS_OK
    assert rec["actionable"] is True
    assert rec["reasons"] == []


def test_run_emits_unresolved_artifact_record(monkeypatch, tmp_path):
    """The ``../../`` class: a configured artifact_path that does not resolve —
    loaded=False, artifact_resolved=False, load_error NAMES the path, FAULT."""
    broken = tmp_path / "does" / "not" / "exist" / "model.pt"
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: broken)
    monkeypatch.setattr(registry, "get", lambda kind: _FailingHandler())

    assert ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                                  "artifact_path": "../../artifacts/prod/model.pt"}])) is None

    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is False
    assert rec["artifact_resolved"] is False
    assert rec["content_sha256"] is None
    assert "../../artifacts/prod/model.pt" in rec["load_error"]
    assert rec["state"] == STATE_UNRESOLVED_ARTIFACT
    assert rec["status"] == STATUS_FAULT
    assert rec["actionable"] is False
    assert rec["reasons"] == ["artifact_unresolved"]


def test_run_emits_stale_record(monkeypatch, tmp_path):
    _wire_loader(monkeypatch, tmp_path, _RecordingXGB(
        {"effective_train_cutoff_date": "2026-01-01", "config_fingerprint": "cfg-1"},
        dict(_FULL_SCORES)))
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[{"name": "s", "kind": "hf_patchtst",
                                  "artifact_path": "model.pt"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is True
    assert rec["state"] == STATE_DEGRADED
    assert rec["actionable"] is False
    assert rec["staleness_days"] > DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS


def test_run_emits_low_coverage_record(monkeypatch, tmp_path):
    _wire_loader(monkeypatch, tmp_path,
                 _RecordingXGB(dict(_FRESH_META), {"AAA": 1.0}))  # BBB/CCC → NaN
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[{"name": "s", "kind": "hf_patchtst",
                                  "artifact_path": "model.pt"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["n_scored"] == 1
    assert rec["coverage_frac"] == pytest.approx(1 / 3)
    assert rec["state"] == STATE_DEGRADED
    assert rec["coverage_frac"] < DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC


def test_run_emits_identity_mismatch_record(monkeypatch, tmp_path):
    """A config-pinned expected digest that disagrees with the file scoring used
    is a FAULT even though the shadow loaded + scored fine."""
    art = _wire_loader(monkeypatch, tmp_path,
                       _RecordingXGB(dict(_FRESH_META), dict(_FULL_SCORES)))
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[{
            "name": "s", "kind": "hf_patchtst", "artifact_path": "model.pt",
            "expected_content_sha256": "sha256:0000000000000000"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["content_sha256"] == content_digest(art)
    assert rec["state"] == STATE_DEGRADED
    assert rec["status"] == STATUS_FAULT
    assert "content_sha256_mismatch" in rec["reasons"]


# ── Early-exit paths each emit a record (no ambiguous silence) ─────────────────

def test_run_disabled_emits_expected_skip(tmp_path):
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_enabled=False,
        shadow_models=[{"name": "s", "kind": "hf_patchtst", "artifact_path": "m"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["state"] == STATE_DISABLED
    assert rec["status"] == STATUS_EXPECTED_SKIP
    assert rec["actionable"] is True
    assert rec["loaded"] is False


def test_run_no_shadow_models_emits_expected_skip(tmp_path):
    ApplyShadowScoringTask().run(_ctx(tmp_path, shadow_models=[]))
    (rec,) = _read_records(tmp_path)
    assert rec["state"] == STATE_NO_SHADOW_MODELS
    assert rec["actionable"] is True


def test_run_no_candidates_emits_per_model_expected_skip(tmp_path):
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, candidates=[],
        shadow_models=[{"name": "a", "kind": "hf_patchtst", "artifact_path": "m"},
                       {"name": "b", "kind": "hf_patchtst", "artifact_path": "m"}]))
    recs = _read_records(tmp_path)
    assert {r["shadow_name"] for r in recs} == {"a", "b"}
    assert all(r["state"] == STATE_NO_CANDIDATES for r in recs)
    assert all(r["actionable"] is True for r in recs)
    assert all(r["reasons"] == ["no_candidates"] for r in recs)


def test_run_no_primary_scores_emits_expected_skip(tmp_path):
    cands = [SimpleNamespace(ticker=t, panel_score=None, rank_score=None) for t in IDX]
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, candidates=cands,
        shadow_models=[{"name": "a", "kind": "hf_patchtst", "artifact_path": "m"}]))
    (rec,) = _read_records(tmp_path)
    assert rec["state"] == STATE_NO_CANDIDATES
    assert rec["reasons"] == ["no_primary_scores"]
    assert rec["actionable"] is True


# ── Health kill switch + one-record-per-model ──────────────────────────────────

def test_run_health_disabled_writes_nothing(monkeypatch, tmp_path):
    _wire_loader(monkeypatch, tmp_path,
                 _RecordingXGB(dict(_FRESH_META), dict(_FULL_SCORES)))
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_health={"enabled": False},
        shadow_models=[{"name": "s", "kind": "hf_patchtst", "artifact_path": "model.pt"}]))
    assert not (tmp_path / "logs" / "shadow_scorer_health.jsonl").exists()


def test_run_emits_one_record_per_shadow_model(monkeypatch, tmp_path):
    _wire_loader(monkeypatch, tmp_path,
                 _RecordingXGB(dict(_FRESH_META), dict(_FULL_SCORES)))
    ApplyShadowScoringTask().run(_ctx(
        tmp_path, shadow_models=[
            {"name": "shadow_a", "kind": "hf_patchtst", "artifact_path": "model.pt"},
            {"name": "shadow_b", "kind": "hf_patchtst", "artifact_path": "model.pt"}]))
    recs = _read_records(tmp_path)
    assert {r["shadow_name"] for r in recs} == {"shadow_a", "shadow_b"}
    assert len(recs) == 2
