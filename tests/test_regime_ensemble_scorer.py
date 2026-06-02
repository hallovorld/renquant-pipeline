"""Tests for RegimeEnsemblePanelScorer (Track C, 2026-06-02 — subrepo mirror).

Mirror of ``tests/test_regime_ensemble_scorer.py`` in the umbrella RenQuant
checkout. The scorer lives under
``renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer`` here.

Test matrix (per Track C plan + ensemble-scorer contract):

  1. ``specialists`` absent → ``load_from_config`` returns the legacy global
     scorer untouched.
  2. All 4 specialists present, high confidence → uses
     ``specialists[final_regime].score(...)`` hard.
  3. 3 of 4 specialists present, high confidence, final_regime missing →
     falls back to the global scorer.
  4. All 4 specialists, low confidence, top-2 posterior — blends top-2.
  5. Specialist with mismatched recipe (new feature col) → raises
     ``StaleSpecialistArtifact`` at load.

References:
  - umbrella tests/test_regime_ensemble_scorer.py
  - doc/research/2026-06-02-bull-calm-signal-recovery-plan.md (Track C)
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


class _FakeScorer:
    def __init__(self, name: str, bias: float, feature_cols: list[str],
                 metadata: dict | None = None):
        self.name = name
        self.bias = float(bias)
        self.feature_cols = list(feature_cols)
        self.metadata = metadata or {}

    def score(self, X: pd.DataFrame) -> pd.Series:
        missing = [c for c in self.feature_cols if c not in X.columns]
        if missing:
            raise KeyError(f"FakeScorer.score: missing {missing}")
        vals = self.bias + X[self.feature_cols].sum(axis=1).values
        return pd.Series(vals, index=X.index, name="panel_score")


@pytest.fixture
def fake_artifacts(tmp_path):
    artifacts: dict[str, tuple[Path, _FakeScorer]] = {}
    feat_cols = ["f1", "f2"]
    for label, bias in [
        ("global", 0.0),
        ("BULL_CALM", 100.0),
        ("BEAR", 200.0),
        ("BULL_VOLATILE", 300.0),
        ("CHOPPY", 400.0),
    ]:
        p = tmp_path / f"panel-ltr.{label}.json"
        p.write_text(json.dumps({"label": label, "feature_cols": feat_cols,
                                  "kind": "panel_ltr_xgboost"}))
        artifacts[label] = (p, _FakeScorer(label, bias, feat_cols,
                                            metadata={"artifact_path": str(p)}))
    return artifacts


@pytest.fixture
def patched_scorer_loader(monkeypatch, fake_artifacts):
    res = pytest.importorskip(
        "renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer",
    )
    by_path = {str(p): sc for (p, sc) in fake_artifacts.values()}

    def fake_load(path):
        p = str(path)
        if p not in by_path:
            raise FileNotFoundError(p)
        return by_path[p]

    monkeypatch.setattr(res.PanelScorer, "load", staticmethod(fake_load))
    return by_path


@pytest.fixture
def feature_matrix():
    return pd.DataFrame(
        {"f1": [1.0, 2.0, 3.0], "f2": [4.0, 5.0, 6.0]},
        index=["AAA", "BBB", "CCC"],
    )


class TestBackCompat:
    def test_no_specialists_returns_global(
        self, patched_scorer_loader, fake_artifacts, feature_matrix,
    ):
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            load_panel_scorer_with_ensemble,
            RegimeEnsemblePanelScorer,
        )
        global_path, global_scorer = fake_artifacts["global"]
        cfg = {"artifact_path": str(global_path)}
        loaded = load_panel_scorer_with_ensemble(cfg)
        assert not isinstance(loaded, RegimeEnsemblePanelScorer)
        assert loaded is global_scorer
        out = loaded.score(feature_matrix)
        assert out.tolist() == [5.0, 7.0, 9.0]


class TestHardPick:
    def _build(self, fake_artifacts, *, regimes: list[str]):
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            load_panel_scorer_with_ensemble,
        )
        global_path, _ = fake_artifacts["global"]
        cfg = {
            "artifact_path": str(global_path),
            "specialists": {r: str(fake_artifacts[r][0]) for r in regimes},
            "specialist_confidence_threshold": 0.8,
        }
        return load_panel_scorer_with_ensemble(cfg)

    def test_all_specialists_high_conf_uses_specialist(
        self, patched_scorer_loader, fake_artifacts, feature_matrix,
    ):
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            RegimeEnsemblePanelScorer,
        )
        ens = self._build(fake_artifacts,
                          regimes=["BULL_CALM", "BEAR", "BULL_VOLATILE", "CHOPPY"])
        assert isinstance(ens, RegimeEnsemblePanelScorer)
        ctx = types.SimpleNamespace(
            final_regime="BULL_CALM",
            regime_confidence=0.9,
            regime_posterior={"BULL_CALM": 0.9, "BEAR": 0.1},
        )
        out = ens.score(feature_matrix, ctx=ctx)
        assert out.tolist() == [105.0, 107.0, 109.0]

    def test_three_of_four_high_conf_missing_specialist_falls_back(
        self, patched_scorer_loader, fake_artifacts, feature_matrix,
    ):
        ens = self._build(fake_artifacts,
                          regimes=["BEAR", "BULL_VOLATILE", "CHOPPY"])
        ctx = types.SimpleNamespace(
            final_regime="BULL_CALM",
            regime_confidence=0.95,
            regime_posterior={"BULL_CALM": 0.95, "BEAR": 0.05},
        )
        out = ens.score(feature_matrix, ctx=ctx)
        assert out.tolist() == [5.0, 7.0, 9.0]


class TestBlend:
    def test_blend_top_two_by_posterior(
        self, patched_scorer_loader, fake_artifacts, feature_matrix,
    ):
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            load_panel_scorer_with_ensemble,
        )
        global_path, _ = fake_artifacts["global"]
        cfg = {
            "artifact_path": str(global_path),
            "specialists": {
                r: str(fake_artifacts[r][0])
                for r in ["BULL_CALM", "BEAR", "BULL_VOLATILE", "CHOPPY"]
            },
            "specialist_confidence_threshold": 0.8,
            "specialist_blend_top_k": 2,
        }
        ens = load_panel_scorer_with_ensemble(cfg)
        ctx = types.SimpleNamespace(
            final_regime="BULL_CALM",
            regime_confidence=0.4,
            regime_posterior={"BULL_CALM": 0.6, "BEAR": 0.3, "CHOPPY": 0.1},
        )
        out = ens.score(feature_matrix, ctx=ctx)
        expected = [
            (100.0 + 5.0) * (0.6 / 0.9) + (200.0 + 5.0) * (0.3 / 0.9),
            (100.0 + 7.0) * (0.6 / 0.9) + (200.0 + 7.0) * (0.3 / 0.9),
            (100.0 + 9.0) * (0.6 / 0.9) + (200.0 + 9.0) * (0.3 / 0.9),
        ]
        np.testing.assert_allclose(out.values, expected, rtol=1e-10)


class TestStaleSpecialist:
    def test_mismatched_recipe_raises(
        self, monkeypatch, fake_artifacts, tmp_path,
    ):
        res = pytest.importorskip(
            "renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer",
        )
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            load_panel_scorer_with_ensemble,
            StaleSpecialistArtifact,
        )
        global_path, global_scorer = fake_artifacts["global"]
        rogue_path = tmp_path / "panel-ltr.rogue.json"
        rogue_path.write_text(json.dumps({"label": "rogue"}))
        rogue = _FakeScorer("rogue", 999.0, ["f1", "f2", "f3"],
                             metadata={"artifact_path": str(rogue_path)})
        by_path = {
            str(global_path): global_scorer,
            str(rogue_path):  rogue,
        }

        def fake_load(path):
            p = str(path)
            if p not in by_path:
                raise FileNotFoundError(p)
            return by_path[p]

        monkeypatch.setattr(res.PanelScorer, "load", staticmethod(fake_load))
        cfg = {
            "artifact_path": str(global_path),
            "specialists": {"BULL_CALM": str(rogue_path)},
        }
        with pytest.raises(StaleSpecialistArtifact, match=r"specialist\[BULL_CALM\]"):
            load_panel_scorer_with_ensemble(cfg)


class TestCtxNoneFallback:
    def test_ctx_none_uses_global(
        self, patched_scorer_loader, fake_artifacts, feature_matrix,
    ):
        from renquant_pipeline.kernel.panel_pipeline.regime_ensemble_scorer import (
            load_panel_scorer_with_ensemble,
        )
        global_path, _ = fake_artifacts["global"]
        cfg = {
            "artifact_path": str(global_path),
            "specialists": {
                "BULL_CALM": str(fake_artifacts["BULL_CALM"][0]),
            },
        }
        ens = load_panel_scorer_with_ensemble(cfg)
        out = ens.score(feature_matrix, ctx=None)
        assert out.tolist() == [5.0, 7.0, 9.0]
