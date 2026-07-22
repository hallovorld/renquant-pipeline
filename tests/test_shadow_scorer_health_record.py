"""Shadow-scorer HEALTH RECORD — the silent-failure sentinel feed.

The shadow panel scorer (PatchTST) is fail-soft: a broken ``../../``
artifact_path makes it load-fail and CONTINUE, so a G4-critical shadow data
feed can die for weeks with nothing but a per-run ``log.warning``. These tests
pin the structured, machine-readable health record that
``ApplyShadowScoringTask`` now emits per configured shadow model per run to
``<strategy_dir>/logs/shadow_scorer_health.jsonl`` so a downstream orchestrator
sentinel can catch the degradation WITHOUT the shadow ever becoming fatal.

Covers: loaded+actionable, unresolved-artifact (``../../`` class), load-failed,
stale train cutoff, low coverage, missing provenance — plus the pure
``finalize_shadow_health`` verdict logic.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import renquant_pipeline.kernel.panel_pipeline.shadow_scoring as shadow_scoring
from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import (
    ApplyShadowScoringTask,
    DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS,
    DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC,
    SHADOW_HEALTH_SCHEMA,
    _new_shadow_health,
    finalize_shadow_health,
    shadow_health_log_path,
)

IDX = ["AAA", "BBB", "CCC"]
RUN_DATE = datetime.date(2026, 7, 21)


@pytest.fixture(autouse=True)
def _clear_scorer_cache():
    """Isolate tests — the module-level scorer cache leaks across runs."""
    shadow_scoring._SCORER_CACHE.clear()
    yield
    shadow_scoring._SCORER_CACHE.clear()


# ── Pure verdict logic (finalize_shadow_health) ────────────────────────────────

def _loaded_health(**over):
    h = _new_shadow_health(
        shadow_name="patchtst_v1", kind="hf_patchtst",
        artifact_path="artifacts/prod/model.pt", run_date=RUN_DATE,
        run_id="run-1", n_candidates=3,
    )
    h["loaded"] = True
    h["artifact_resolved"] = True
    h["effective_train_cutoff_date"] = "2026-07-10"   # 11d stale → fresh
    h["config_fingerprint"] = "cfg-abc"
    h["n_scored"] = 3
    h["coverage_frac"] = 1.0
    h.update(over)
    return h


def test_finalize_actionable_when_fresh_covered_and_provenanced():
    h = finalize_shadow_health(_loaded_health(), run_date=RUN_DATE)
    assert h["actionable"] is True
    assert h["reasons"] == []
    assert h["staleness_days"] == 11


def test_finalize_flags_stale_train_cutoff():
    h = finalize_shadow_health(
        _loaded_health(effective_train_cutoff_date="2026-01-01"),
        run_date=RUN_DATE,
    )
    assert h["actionable"] is False
    assert any(r.startswith("stale_") for r in h["reasons"])
    assert h["staleness_days"] == (RUN_DATE - datetime.date(2026, 1, 1)).days


def test_finalize_flags_low_coverage():
    h = finalize_shadow_health(
        _loaded_health(n_scored=1, coverage_frac=1 / 3),
        run_date=RUN_DATE,
    )
    assert h["actionable"] is False
    assert any(r.startswith("low_coverage_") for r in h["reasons"])


def test_finalize_flags_missing_provenance():
    h = finalize_shadow_health(
        _loaded_health(effective_train_cutoff_date=None, config_fingerprint=None),
        run_date=RUN_DATE,
    )
    assert h["actionable"] is False
    assert "missing_train_cutoff" in h["reasons"]
    assert "missing_config_fingerprint" in h["reasons"]
    assert h["staleness_days"] is None


def test_finalize_flags_future_cutoff():
    h = finalize_shadow_health(
        _loaded_health(effective_train_cutoff_date="2026-09-01"),
        run_date=RUN_DATE,
    )
    assert h["actionable"] is False
    assert any(r.startswith("train_cutoff_future_") for r in h["reasons"])


def test_finalize_not_loaded_unresolved_artifact():
    h = _new_shadow_health(
        shadow_name="s", kind="hf_patchtst", artifact_path="../../bad.pt",
        run_date=RUN_DATE, run_id="r", n_candidates=3,
    )
    h["artifact_resolved"] = False
    finalize_shadow_health(h, run_date=RUN_DATE)
    assert h["actionable"] is False
    assert h["reasons"] == ["artifact_unresolved"]
    assert h["staleness_days"] is None


def test_finalize_not_loaded_but_resolved_is_load_failed():
    h = _new_shadow_health(
        shadow_name="s", kind="hf_patchtst", artifact_path="ok.pt",
        run_date=RUN_DATE, run_id="r", n_candidates=3,
    )
    h["artifact_resolved"] = True   # file exists but load raised
    finalize_shadow_health(h, run_date=RUN_DATE)
    assert h["reasons"] == ["load_failed"]


def test_finalize_zero_scores_reports_skip_reason():
    h = _loaded_health(n_scored=0, coverage_frac=0.0,
                       skip_reason="degenerate_cross_section")
    finalize_shadow_health(h, run_date=RUN_DATE)
    assert "degenerate_cross_section" in h["reasons"]


# ── Sink path resolution ───────────────────────────────────────────────────────

def test_log_path_defaults_under_strategy_dir(tmp_path):
    p = shadow_health_log_path({"_strategy_dir": str(tmp_path)})
    assert p == tmp_path / "logs" / "shadow_scorer_health.jsonl"


def test_log_path_honours_override():
    p = shadow_health_log_path({"shadow_health": {"path": "/x/y/health.jsonl"}})
    assert p == Path("/x/y/health.jsonl")


# ── Task-level integration (writes the JSONL sink) ─────────────────────────────

class _RecordingXGB:
    """Loaded non-history (xgb) shadow scorer with configurable provenance."""
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


def _ctx(tmp_path, *, matrix, shadow_models):
    cands = [SimpleNamespace(ticker=t, panel_score=float(i + 1), rank_score=None)
             for i, t in enumerate(IDX)]
    return SimpleNamespace(
        config={"ranking": {"panel_scoring": {
            "shadow_models": shadow_models,
            "shadow_log_mlflow": False,
            "kind": "hf_patchtst",
        }}, "_strategy_dir": str(tmp_path)},
        candidates=cands,
        _panel_matrix=matrix,
        today=RUN_DATE,
        run_id="run-xyz",
        holdings={},
        regime="BULL_CALM",
        counters={},
    )


def _read_records(tmp_path):
    sink = tmp_path / "logs" / "shadow_scorer_health.jsonl"
    assert sink.exists(), "health JSONL sink was not written"
    return [json.loads(line) for line in sink.read_text().splitlines() if line]


_VARIED = pd.DataFrame({"KMID": [0.1, 0.5, 0.9], "KLEN": [1.0, 2.0, 3.0]}, index=IDX)


def test_run_emits_actionable_record_for_healthy_shadow(monkeypatch, tmp_path):
    art = tmp_path / "model.pt"
    art.write_text("x")   # resolves to an existing file
    scorer = _RecordingXGB(
        metadata={"effective_train_cutoff_date": "2026-07-10",
                  "config_fingerprint": "cfg-1"},
        scores={"AAA": 3.0, "BBB": 2.0, "CCC": 1.0},
    )
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: art)
    monkeypatch.setattr(registry, "get", lambda kind: _LoaderHandler(scorer))

    ApplyShadowScoringTask().run(_ctx(
        tmp_path, matrix=_VARIED,
        shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                        "artifact_path": "model.pt"}]))

    (rec,) = _read_records(tmp_path)
    assert rec["schema"] == SHADOW_HEALTH_SCHEMA
    assert rec["shadow_name"] == "patchtst_v1"
    assert rec["run_id"] == "run-xyz"
    assert rec["run_date"] == RUN_DATE.isoformat()
    assert rec["loaded"] is True
    assert rec["artifact_resolved"] is True
    assert rec["load_error"] is None
    assert rec["effective_train_cutoff_date"] == "2026-07-10"
    assert rec["staleness_days"] == 11
    assert rec["config_fingerprint"] == "cfg-1"
    assert rec["n_candidates"] == 3
    assert rec["n_scored"] == 3
    assert rec["coverage_frac"] == pytest.approx(1.0)
    assert rec["actionable"] is True
    assert rec["reasons"] == []


def test_run_emits_unresolved_artifact_record(monkeypatch, tmp_path):
    """The exact ``../../`` class: a configured artifact_path that does not
    resolve to an existing file — loaded=False, artifact_resolved=False,
    load_error NAMES the path, and the shadow is NOT actionable."""
    broken = tmp_path / "does" / "not" / "exist" / "model.pt"   # never created
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: broken)
    monkeypatch.setattr(registry, "get", lambda kind: _FailingHandler())

    # Must NOT raise — shadow failure stays non-fatal.
    assert ApplyShadowScoringTask().run(_ctx(
        tmp_path, matrix=_VARIED,
        shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                        "artifact_path": "../../artifacts/prod/model.pt"}])) is None

    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is False
    assert rec["artifact_resolved"] is False
    assert "../../artifacts/prod/model.pt" in rec["load_error"]
    assert "does not resolve" in rec["load_error"]
    assert rec["actionable"] is False
    assert rec["reasons"] == ["artifact_unresolved"]


def test_run_emits_stale_record(monkeypatch, tmp_path):
    art = tmp_path / "model.pt"
    art.write_text("x")
    scorer = _RecordingXGB(
        metadata={"effective_train_cutoff_date": "2026-01-01",   # ~201d stale
                  "config_fingerprint": "cfg-1"},
        scores={"AAA": 3.0, "BBB": 2.0, "CCC": 1.0},
    )
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: art)
    monkeypatch.setattr(registry, "get", lambda kind: _LoaderHandler(scorer))

    ApplyShadowScoringTask().run(_ctx(
        tmp_path, matrix=_VARIED,
        shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                        "artifact_path": "model.pt"}]))

    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is True
    assert rec["actionable"] is False
    assert rec["staleness_days"] > DEFAULT_SHADOW_HEALTH_MAX_STALENESS_DAYS
    assert any(r.startswith("stale_") for r in rec["reasons"])


def test_run_emits_low_coverage_record(monkeypatch, tmp_path):
    art = tmp_path / "model.pt"
    art.write_text("x")
    # Only AAA gets a finite score; BBB/CCC → NaN → coverage 1/3 < 0.80.
    scorer = _RecordingXGB(
        metadata={"effective_train_cutoff_date": "2026-07-10",
                  "config_fingerprint": "cfg-1"},
        scores={"AAA": 1.0},
    )
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: art)
    monkeypatch.setattr(registry, "get", lambda kind: _LoaderHandler(scorer))

    ApplyShadowScoringTask().run(_ctx(
        tmp_path, matrix=_VARIED,
        shadow_models=[{"name": "patchtst_v1", "kind": "hf_patchtst",
                        "artifact_path": "model.pt"}]))

    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is True
    assert rec["n_scored"] == 1
    assert rec["coverage_frac"] == pytest.approx(1 / 3)
    assert rec["actionable"] is False
    assert any(r.startswith("low_coverage_") for r in rec["reasons"])
    assert rec["coverage_frac"] < DEFAULT_SHADOW_HEALTH_MIN_COVERAGE_FRAC


def test_run_emits_one_record_per_shadow_model(monkeypatch, tmp_path):
    art = tmp_path / "model.pt"
    art.write_text("x")
    scorer = _RecordingXGB(
        metadata={"effective_train_cutoff_date": "2026-07-10",
                  "config_fingerprint": "cfg-1"},
        scores={"AAA": 3.0, "BBB": 2.0, "CCC": 1.0},
    )
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    monkeypatch.setattr(shadow_scoring, "_resolve_shadow_artifact_path",
                        lambda *a, **k: art)
    monkeypatch.setattr(registry, "get", lambda kind: _LoaderHandler(scorer))

    ApplyShadowScoringTask().run(_ctx(
        tmp_path, matrix=_VARIED,
        shadow_models=[
            {"name": "shadow_a", "kind": "hf_patchtst", "artifact_path": "model.pt"},
            {"name": "shadow_b", "kind": "hf_patchtst", "artifact_path": "model.pt"},
        ]))

    recs = _read_records(tmp_path)
    assert {r["shadow_name"] for r in recs} == {"shadow_a", "shadow_b"}
    assert len(recs) == 2
