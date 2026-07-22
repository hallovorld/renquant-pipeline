"""Guard: a NON-history shadow scorer must be skipped (not collapsed) when it is
handed a degenerate ``ctx._panel_matrix``.

2026-06-26. On history-primary runs (e.g. hf_patchtst), the xgb-rows block in
job_panel_scoring that stamps a valid per-ticker cross-section into
``ctx._panel_matrix`` does not run, so a non-history (xgb) shadow scorer receives
a constant input for every ticker → collapsed prediction → ``model_contract``
HARD FAIL (``pct_zero_var_cols=100%``). That is a meaningless comparison, not a
model fault (shadow-only, zero live impact). The guard skips it with a WARNING.

It surfaced live on 2026-06-26 only because the shadow e2e run finally completed
end-to-end (prior days short-circuited on stale state) and first reached the
legacy ``xgb_alpha158_fund_previous_primary`` shadow under the hf_patchtst
primary.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import renquant_pipeline.kernel.panel_pipeline.shadow_scoring as shadow_scoring
from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import (
    ApplyShadowScoringTask,
    _is_degenerate_cross_section,
)

IDX = ["AAA", "BBB", "CCC"]


def test_is_degenerate_cross_section_thresholds():
    # all columns constant → degenerate
    const = pd.DataFrame({"a": [1.0, 1.0, 1.0], "b": [2.0, 2.0, 2.0]}, index=IDX)
    assert _is_degenerate_cross_section(const) is True
    # >50% constant (2 of 3) → degenerate
    deg = pd.DataFrame({"a": [1, 1, 1], "b": [2, 2, 2], "c": [1, 2, 3]},
                       index=IDX, dtype=float)
    assert _is_degenerate_cross_section(deg) is True
    # <=50% constant (1 of 3) → fine
    ok = pd.DataFrame({"a": [1, 1, 1], "b": [2, 3, 4], "c": [1, 2, 3]},
                      index=IDX, dtype=float)
    assert _is_degenerate_cross_section(ok) is False
    # fully varied → fine
    varied = pd.DataFrame({"a": [0.1, 0.5, 0.9], "b": [1.0, 2.0, 3.0]}, index=IDX)
    assert _is_degenerate_cross_section(varied) is False
    # single row → cannot assess variance → not flagged
    one = pd.DataFrame({"a": [1.0], "b": [2.0]}, index=["AAA"])
    assert _is_degenerate_cross_section(one) is False


class _RecordingXGB:
    """Stand-in for a loaded non-history (xgb) shadow scorer."""
    requires_history = False
    feature_cols = ["KMID", "KLEN"]
    metadata = {"kind": "xgb"}

    def __init__(self):
        self.called = False

    def score(self, X):
        self.called = True
        return pd.Series({t: 0.0 for t in X.index}, dtype=float)


def _ctx(matrix: pd.DataFrame) -> SimpleNamespace:
    cands = [SimpleNamespace(ticker=t, panel_score=float(i + 1), rank_score=None)
             for i, t in enumerate(IDX)]
    return SimpleNamespace(
        config={"ranking": {"panel_scoring": {
            "shadow_models": [{"name": "xgb_shadow", "kind": "xgb",
                               "artifact_path": "dummy"}],
            "shadow_log_mlflow": False,
            "kind": "hf_patchtst",
        }}, "_strategy_dir": None},
        candidates=cands,
        _panel_matrix=matrix,
        today=pd.Timestamp("2026-06-26"),
        holdings={},
        regime="BULL_CALM",
        counters={},
    )


def _wire(monkeypatch, scorer):
    """Pre-seed the scorer cache + stub artifact/registry so run() reaches the
    score step without touching the filesystem or model registry.

    Resolution goes through the single ``resolve_artifact_identity`` authority, so
    force it to a RESOLVED identity for the ``dummy`` ref (resolved_path=``dummy``
    → cache hit) — the run then bypasses real loading and exercises the degenerate
    cross-section guard, which is what this test is about."""
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    from renquant_pipeline.kernel.panel_pipeline.shadow_health import ArtifactIdentity
    monkeypatch.setattr(
        shadow_scoring, "resolve_artifact_identity",
        lambda *a, **k: ArtifactIdentity(
            ref="dummy", resolved=True, resolved_path="dummy",
            source="strategy_dir", content_sha256="sha256:deadbeefdeadbeef",
            error=None))
    monkeypatch.setattr(registry, "get", lambda kind: object())
    shadow_scoring._SCORER_CACHE[("xgb", "dummy")] = scorer


def test_degenerate_matrix_skips_nonhistory_shadow(monkeypatch):
    rec = _RecordingXGB()
    _wire(monkeypatch, rec)
    try:
        matrix = pd.DataFrame({"KMID": [1.0, 1.0, 1.0], "KLEN": [2.0, 2.0, 2.0]},
                              index=IDX)
        ApplyShadowScoringTask().run(_ctx(matrix))
        assert rec.called is False  # degenerate cross-section → skipped, never scored
    finally:
        shadow_scoring._SCORER_CACHE.pop(("xgb", "dummy"), None)


def test_varied_matrix_still_scores_nonhistory_shadow(monkeypatch):
    rec = _RecordingXGB()
    _wire(monkeypatch, rec)
    try:
        matrix = pd.DataFrame({"KMID": [0.1, 0.5, 0.9], "KLEN": [1.0, 2.0, 3.0]},
                              index=IDX)
        ApplyShadowScoringTask().run(_ctx(matrix))
        assert rec.called is True  # real cross-section → guard does NOT over-fire
    finally:
        shadow_scoring._SCORER_CACHE.pop(("xgb", "dummy"), None)
