"""coverage_frac must measure coverage OF THE CANDIDATE SET — not the width of
whatever matrix the shadow scored.

orch#727, measured live: the clf lane scored 322 names against 292 candidates and
`coverage_frac = n_scored / n_candidates` exceeded 1.0 on every session since
go-live (12/12 records fault, peak 1.1039), because the numerator counted finite
scores over the shadow's own matrix while the denominator counted the primary's
candidates. The fixed numerator is the intersection; the raw breadth stays
observable as ``n_scored_total``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import renquant_pipeline.kernel.panel_pipeline.shadow_scoring as shadow_scoring
from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import (
    ApplyShadowScoringTask,
)

CANDS = ["AAA", "BBB", "CCC"]
WIDE = CANDS + ["DDD", "EEE"]


class _WideXGB:
    """Scores every row of the matrix it is handed — wider than the candidates."""
    requires_history = False
    feature_cols = ["KMID", "KLEN"]
    metadata = {"kind": "xgb"}

    def score(self, X):
        return pd.Series({t: 0.1 * (i + 1) for i, t in enumerate(X.index)},
                         dtype=float)


class _NonCandidateXGB:
    """Scores ONLY names outside the candidate set (coverage of candidates = 0)."""
    requires_history = False
    feature_cols = ["KMID", "KLEN"]
    metadata = {"kind": "xgb"}

    def score(self, X):
        return pd.Series({"DDD": 0.1, "EEE": 0.2}, dtype=float)


def _ctx(matrix: pd.DataFrame) -> SimpleNamespace:
    cands = [SimpleNamespace(ticker=t, panel_score=float(i + 1), rank_score=None)
             for i, t in enumerate(CANDS)]
    return SimpleNamespace(
        config={"ranking": {"panel_scoring": {
            "shadow_models": [{"name": "xgb_shadow", "kind": "xgb",
                               "artifact_path": "dummy"}],
            "shadow_log_mlflow": False,
            "kind": "hf_patchtst",
        }}, "_strategy_dir": None},
        candidates=cands,
        _panel_matrix=matrix,
        today=pd.Timestamp("2026-08-01"),
        holdings={},
        regime="BULL_CALM",
        counters={},
    )


def _wire(monkeypatch, scorer, captured):
    from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
    from renquant_pipeline.kernel.panel_pipeline.shadow_health import ArtifactIdentity
    monkeypatch.setattr(
        shadow_scoring, "resolve_artifact_identity",
        lambda *a, **k: ArtifactIdentity(
            ref="dummy", resolved=True, resolved_path="dummy",
            source="strategy_dir", content_sha256="sha256:deadbeefdeadbeef",
            error=None))
    monkeypatch.setattr(registry, "get", lambda kind: object())
    monkeypatch.setattr(shadow_scoring, "shadow_health_sink_defined",
                        lambda cfg: True)
    monkeypatch.setattr(shadow_scoring, "append_shadow_health",
                        lambda sink, rec: captured.append(rec))
    shadow_scoring._SCORER_CACHE[("xgb", "dummy")] = scorer


def _varied_matrix(index):
    return pd.DataFrame({"KMID": [0.1 * (i + 1) for i in range(len(index))],
                         "KLEN": [1.0 * (i + 1) for i in range(len(index))]},
                        index=index)


def test_wider_shadow_matrix_caps_coverage_at_the_candidate_set(monkeypatch):
    captured: list[dict] = []
    _wire(monkeypatch, _WideXGB(), captured)
    try:
        ApplyShadowScoringTask().run(_ctx(_varied_matrix(WIDE)))
    finally:
        shadow_scoring._SCORER_CACHE.pop(("xgb", "dummy"), None)
    assert len(captured) == 1
    rec = captured[0]
    assert rec["n_candidates"] == 3
    assert rec["n_scored"] == 3            # candidates covered
    assert rec["n_scored_total"] == 5      # raw breadth stays observable
    assert rec["coverage_frac"] == 1.0     # a fraction again, never > 1


def test_shadow_scoring_only_noncandidates_is_zero_coverage_fault(monkeypatch):
    captured: list[dict] = []
    _wire(monkeypatch, _NonCandidateXGB(), captured)
    try:
        ApplyShadowScoringTask().run(_ctx(_varied_matrix(WIDE)))
    finally:
        shadow_scoring._SCORER_CACHE.pop(("xgb", "dummy"), None)
    assert len(captured) == 1
    rec = captured[0]
    assert rec["n_scored"] == 0 and rec["coverage_frac"] == 0.0
    assert rec["n_scored_total"] == 2
    assert rec["status"] == "fault"        # zero candidate coverage is a fault
