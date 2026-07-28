"""Tests for the composite BLEND scorer kind — certified z(prod) + z(clf).

Blend construction: renquant-model#74/75/76 confirmatory line (prereg
model#75); design reference pipeline#213
(doc/design/2026-07-25-blend-shadow-deployment.md). Scorer:
``kernel/panel_pipeline/blend_scorer.py``; dispatch: ``model_registry``
kind ``blend``.

Test matrix (mirrors the scorer-contract test conventions —
test_xgboost_scorer_contract / test_regime_ensemble_scorer /
test_panel_scoring_specialist_wiring):

  1. Registry dispatch — kind "blend" registered; loader returns
     BlendPanelScorer; train_cmd refuses (inference-only composition).
  2. Both-pin verification, fail-closed at load — abbrev + full content
     pins and both config-fp written forms ACCEPT; content mismatch /
     fp mismatch / missing pin key / short pin / wrong component count /
     unresolvable file / history-requiring component all RAISE.
  3. z-sum math vs a hand-computed fixture (ddof=0), plus parity of the
     real-artifact composite against a manually composed
     z(prod) + z(clf) with per-component raw→model transforms.
  4. Degenerate-cross-section guard — std==0 or <2 scored names →
     component contributes 0 and metadata.degraded_reason records it
     (fail SOFT within the composite); resets on a healthy call.
  5. Interface parity with PanelScorer — union feature_cols,
     requires_history False, Series-out contract, ctx-kwarg uniformity,
     KeyError on missing union columns.
  6. Metadata — both component identities, effective_train_cutoff_date =
     the OLDER component (conservative), deterministic + order-sensitive
     composite config_fingerprint.
  7. Kernel wiring — LoadScorerTask dispatches kind=blend (path anchor on
     component 0 without a top-level artifact_path), fails closed on a
     pin mismatch, and the kind!=blend default stays byte-identical;
     ApplyScoresTask routes blend through the alpha158-rebuild branch and
     passes the RAW union matrix (no outer transform).
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

xgb = pytest.importorskip("xgboost")

from renquant_pipeline.kernel.panel_pipeline.blend_scorer import (  # noqa: E402
    BlendComponent,
    BlendPanelScorer,
    composite_config_fingerprint,
    config_fp_pin_matches,
    content_pin_matches,
    load_blend_scorer,
)

FEAT_COLS = ["f1", "f2"]
PROD_FP = "sha256:f8fb2259b2bf1537"                      # 16-hex short form
CLF_FP = "sha256:" + "1d8f167fed18cd8c" * 4              # full 64-hex form


# ── fixtures ─────────────────────────────────────────────────────────────────

def _train_booster(seed: int):
    dtrain = xgb.DMatrix(
        [[1.0, 0.2], [0.8, 0.1], [-1.0, 0.0], [-0.7, -0.1]],
        label=[1.0, 0.8, -1.0, -0.8],
    )
    return xgb.train(
        {"objective": "reg:squarederror", "max_depth": 2, "eta": 0.7,
         "nthread": 1, "verbosity": 0, "seed": seed},
        dtrain, num_boost_round=6, verbose_eval=False,
    )


def _write_component(path: Path, *, fp: str, trained_date: str, seed: int,
                     feature_means: list[float] | None = None,
                     feature_stds: list[float] | None = None) -> str:
    """Write a PanelScorer-loadable xgb artifact; return its FULL file sha."""
    booster = _train_booster(seed)
    payload = {
        "version": 2,
        "kind": "panel_ltr_xgboost",
        "feature_cols": list(FEAT_COLS),
        "config_fingerprint": fp,
        "trained_date": trained_date,
        "booster_raw_json": bytes(
            booster.save_raw(raw_format="json")).decode("utf-8"),
    }
    if feature_means is not None:
        payload["feature_means"] = feature_means
        payload["feature_stds"] = feature_stds
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def component_artifacts(tmp_path):
    """(config, sha_prod, sha_clf) for a valid two-component blend setup.

    Component 0 (prod-like) carries feature_means/stds — exercising the
    per-leg raw→model transform; component 1 (clf-like) is stat-less.
    """
    sha_prod = _write_component(
        tmp_path / "prod.json", fp=PROD_FP, trained_date="2026-06-21", seed=7,
        feature_means=[0.5, 0.1], feature_stds=[2.0, 0.5])
    sha_clf = _write_component(
        tmp_path / "clf.json", fp=CLF_FP, trained_date="2026-07-26", seed=99)
    config = {
        "ranking": {"panel_scoring": {
            "enabled": True,
            "kind": "blend",
            "components": [
                {"artifact_path": str(tmp_path / "prod.json"),
                 # abbreviated 16-hex pin, prefixed — the shadow_models
                 # convention (content=abbrev)
                 "expected_content_sha256": "sha256:" + sha_prod[:16],
                 "expected_config_fingerprint": PROD_FP},
                {"artifact_path": str(tmp_path / "clf.json"),
                 # full digest, bare form — must ALSO verify
                 "expected_content_sha256": sha_clf,
                 # fp pinned in the bare form vs stored prefixed form
                 "expected_config_fingerprint": CLF_FP.split(":", 1)[1]},
            ],
        }},
        "_strategy_dir": None,
    }
    return config, sha_prod, sha_clf


class _FakeScorer:
    """Deterministic column-projection scorer (regime-ensemble test style)."""

    requires_history = False

    def __init__(self, feature_cols: list[str], project_col: str,
                 scale: float = 1.0, metadata: dict | None = None):
        self.feature_cols = list(feature_cols)
        self.project_col = project_col
        self.scale = float(scale)
        self.metadata = dict(metadata or {})

    def score(self, X: pd.DataFrame, ctx=None) -> pd.Series:  # noqa: ARG002
        missing = [c for c in self.feature_cols if c not in X.columns]
        if missing:
            raise KeyError(f"_FakeScorer.score: missing {missing}")
        return pd.Series(self.scale * X[self.project_col].to_numpy(dtype=float),
                         index=X.index, name="panel_score")


def _fake_component(scorer, i: int, trained: str | None = "2026-06-21"):
    return BlendComponent(
        scorer=scorer,
        artifact_path=f"/fake/component{i}.json",
        content_sha256="sha256:" + ("ab" * 32),
        config_fingerprint=f"sha256:fp{i}",
        trained_date=trained,
        effective_train_cutoff_date=trained,
    )


def _z(v: np.ndarray) -> np.ndarray:
    return (v - v.mean()) / v.std()  # ddof=0 — numpy default


# ── 1. registry dispatch ─────────────────────────────────────────────────────

class TestRegistryDispatch:
    def test_blend_kind_registered(self):
        from renquant_pipeline.kernel.panel_pipeline.model_registry import registry

        assert "blend" in registry.list()
        handler = registry.get("blend")
        assert handler.requires_history is False

    def test_scorer_loader_builds_blend(self, component_artifacts):
        from renquant_pipeline.kernel.panel_pipeline.model_registry import registry

        config, _, _ = component_artifacts
        scorer = registry.get("blend").scorer_loader(None, config)
        assert isinstance(scorer, BlendPanelScorer)
        assert scorer.metadata["kind"] == "blend"

    def test_train_cmd_refuses(self):
        from renquant_pipeline.kernel.panel_pipeline.model_registry import registry

        with pytest.raises(NotImplementedError):
            registry.get("blend").train_cmd(SimpleNamespace())


# ── 2. both-pin verification, fail-closed ────────────────────────────────────

class TestPinVerification:
    def test_valid_pins_load(self, component_artifacts):
        config, sha_prod, sha_clf = component_artifacts
        scorer = load_blend_scorer(config)
        comps = scorer.metadata["components"]
        assert comps[0]["content_sha256"] == "sha256:" + sha_prod
        assert comps[1]["content_sha256"] == "sha256:" + sha_clf

    def test_content_pin_mismatch_raises(self, component_artifacts):
        config, _, _ = component_artifacts
        bad = dict(config["ranking"]["panel_scoring"]["components"][1])
        bad["expected_content_sha256"] = "sha256:" + "0" * 16
        config["ranking"]["panel_scoring"]["components"][1] = bad
        with pytest.raises(ValueError, match="content_sha256 MISMATCH"):
            load_blend_scorer(config)

    def test_config_fp_pin_mismatch_raises(self, component_artifacts):
        config, _, _ = component_artifacts
        bad = dict(config["ranking"]["panel_scoring"]["components"][0])
        bad["expected_config_fingerprint"] = "sha256:deadbeefdeadbeef"
        config["ranking"]["panel_scoring"]["components"][0] = bad
        with pytest.raises(ValueError, match="config_fingerprint MISMATCH"):
            load_blend_scorer(config)

    @pytest.mark.parametrize("key", [
        "artifact_path", "expected_content_sha256", "expected_config_fingerprint",
    ])
    def test_missing_pin_key_raises(self, component_artifacts, key):
        config, _, _ = component_artifacts
        entry = dict(config["ranking"]["panel_scoring"]["components"][0])
        entry.pop(key)
        config["ranking"]["panel_scoring"]["components"][0] = entry
        with pytest.raises(ValueError, match="missing required key"):
            load_blend_scorer(config)

    def test_too_short_content_pin_rejected(self, component_artifacts):
        config, sha_prod, _ = component_artifacts
        entry = dict(config["ranking"]["panel_scoring"]["components"][0])
        entry["expected_content_sha256"] = "sha256:" + sha_prod[:6]  # < 8 hex
        config["ranking"]["panel_scoring"]["components"][0] = entry
        with pytest.raises(ValueError, match="content_sha256 MISMATCH"):
            load_blend_scorer(config)

    @pytest.mark.parametrize("n", [0, 1, 3])
    def test_component_count_enforced(self, component_artifacts, n):
        config, _, _ = component_artifacts
        comps = config["ranking"]["panel_scoring"]["components"]
        config["ranking"]["panel_scoring"]["components"] = (comps * 2)[:n]
        with pytest.raises(ValueError, match="exactly 2"):
            load_blend_scorer(config)

    def test_unresolvable_component_fails_closed(self, component_artifacts, tmp_path):
        config, _, _ = component_artifacts
        entry = dict(config["ranking"]["panel_scoring"]["components"][1])
        entry["artifact_path"] = str(tmp_path / "nope.json")
        config["ranking"]["panel_scoring"]["components"][1] = entry
        with pytest.raises(FileNotFoundError):
            load_blend_scorer(config)

    def test_history_component_rejected(self, component_artifacts, monkeypatch):
        from renquant_pipeline.kernel.panel_pipeline import panel_scorer as ps

        config, _, _ = component_artifacts
        fake = SimpleNamespace(
            requires_history=True, feature_cols=FEAT_COLS,
            metadata={"config_fingerprint": PROD_FP})
        monkeypatch.setattr(ps.PanelScorer, "load",
                            staticmethod(lambda path: fake))
        with pytest.raises(ValueError, match="history-requiring"):
            load_blend_scorer(config)

    def test_pin_matchers_unit(self):
        full = "a" * 64
        assert content_pin_matches("sha256:" + full[:16], full)
        assert content_pin_matches(full, "sha256:" + full)
        assert content_pin_matches("SHA256:" + full[:16].upper(), full)
        assert not content_pin_matches("sha256:" + full[:6], full)  # too short
        assert not content_pin_matches("b" * 16, full)
        assert not content_pin_matches(None, full)
        assert config_fp_pin_matches("sha256:abc123", "abc123")
        assert config_fp_pin_matches("abc123", "sha256:abc123")
        assert config_fp_pin_matches("sha256:abc123", "sha256:abc123")
        assert not config_fp_pin_matches("sha256:abc123", "sha256:abc124")
        assert not config_fp_pin_matches("", "abc123")
        assert not config_fp_pin_matches(None, "abc123")


# ── 3. z-sum math ────────────────────────────────────────────────────────────

class TestZSumMath:
    def test_hand_computed_fixture(self):
        # comp0 projects f1 = [1,2,3,4]; comp1 projects f2 = [1,1,2,4].
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        X = pd.DataFrame(
            {"f1": [1.0, 2.0, 3.0, 4.0], "f2": [1.0, 1.0, 2.0, 4.0]},
            index=["AAA", "BBB", "CCC", "DDD"],
        )
        out = blend.score(X)
        expected = _z(np.array([1.0, 2.0, 3.0, 4.0])) + \
            _z(np.array([1.0, 1.0, 2.0, 4.0]))
        np.testing.assert_allclose(out.to_numpy(), expected, rtol=1e-12)
        # spot-check one literal (ddof=0): z_f1[0] = (1-2.5)/sqrt(1.25),
        # z_f2[0] = (1-2)/sqrt(1.5)
        assert out.iloc[0] == pytest.approx(
            (1.0 - 2.5) / np.sqrt(1.25) + (1.0 - 2.0) / np.sqrt(1.5))
        assert blend.metadata["degraded_reason"] is None

    def test_real_artifact_parity_with_manual_composition(
            self, component_artifacts):
        """Composite == z(prod) + z(clf) with each leg's OWN raw→model
        transform — proves the per-component transform is applied (comp 0
        stores feature_means/stds; skipping them would change its scores)."""
        from renquant_pipeline.kernel.panel_pipeline.feature_transform import (
            transform_feature_frame,
        )
        from renquant_pipeline.kernel.panel_pipeline.panel_scorer import PanelScorer

        config, _, _ = component_artifacts
        blend = load_blend_scorer(config)
        rng = np.random.RandomState(3)
        X = pd.DataFrame(rng.uniform(-2, 2, size=(6, 2)), columns=FEAT_COLS,
                         index=[f"T{i}" for i in range(6)])
        out = blend.score(X)

        comps = config["ranking"]["panel_scoring"]["components"]
        expected = np.zeros(len(X))
        for entry in comps:
            leg = PanelScorer.load(entry["artifact_path"])
            x_leg = transform_feature_frame(
                X, leg.feature_cols, leg.metadata, source_space="raw")
            v = leg.score(x_leg).to_numpy(dtype=float)
            expected = expected + _z(v)
        np.testing.assert_allclose(out.to_numpy(), expected, rtol=1e-10)

    def test_nan_component_score_propagates(self):
        class _NaNScorer(_FakeScorer):
            def score(self, X, ctx=None):  # noqa: ARG002
                s = super().score(X)
                s.iloc[0] = np.nan
                return s

        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_NaNScorer(FEAT_COLS, "f2"), 1),
        ])
        X = pd.DataFrame(
            {"f1": [1.0, 2.0, 3.0, 4.0], "f2": [1.0, 1.0, 2.0, 4.0]},
            index=["AAA", "BBB", "CCC", "DDD"],
        )
        out = blend.score(X)
        assert np.isnan(out.iloc[0])           # unscored name drops downstream
        finite = np.array([1.0, 2.0, 4.0])     # comp1 z over its FINITE universe
        z1 = (finite - finite.mean()) / finite.std()
        z0 = _z(np.array([1.0, 2.0, 3.0, 4.0]))
        np.testing.assert_allclose(
            out.to_numpy()[1:], z0[1:] + z1, rtol=1e-12)


# ── 4. degenerate guard ──────────────────────────────────────────────────────

class TestDegenerateGuard:
    def test_zero_std_component_contributes_zero(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        X = pd.DataFrame(
            {"f1": [1.0, 2.0, 3.0, 4.0], "f2": [5.0, 5.0, 5.0, 5.0]},
            index=["AAA", "BBB", "CCC", "DDD"],
        )
        out = blend.score(X)
        np.testing.assert_allclose(
            out.to_numpy(), _z(np.array([1.0, 2.0, 3.0, 4.0])), rtol=1e-12)
        reasons = blend.metadata["degraded_reason"]
        assert reasons is not None and len(reasons) == 1
        assert "component1" in reasons[0] and "std_zero" in reasons[0]

    def test_single_name_universe_contributes_zero(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        X = pd.DataFrame({"f1": [1.0], "f2": [2.0]}, index=["AAA"])
        out = blend.score(X)
        assert out.to_numpy().tolist() == [0.0]
        reasons = blend.metadata["degraded_reason"]
        assert len(reasons) == 2
        assert all("n_lt_2" in r for r in reasons)

    def test_degraded_reason_resets_on_healthy_call(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        X_bad = pd.DataFrame({"f1": [1.0], "f2": [2.0]}, index=["AAA"])
        blend.score(X_bad)
        assert blend.metadata["degraded_reason"] is not None
        X_ok = pd.DataFrame(
            {"f1": [1.0, 2.0], "f2": [3.0, 1.0]}, index=["AAA", "BBB"])
        blend.score(X_ok)
        assert blend.metadata["degraded_reason"] is None


# ── 5. interface parity with PanelScorer ─────────────────────────────────────

class TestInterfaceParity:
    def test_feature_cols_union_sorted(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(["b", "a"], "a"), 0),
            _fake_component(_FakeScorer(["c", "a"], "c"), 1),
        ])
        assert blend.feature_cols == ["a", "b", "c"]
        assert blend.requires_history is False
        assert blend.seq_len == 1

    def test_score_series_contract(self, component_artifacts):
        config, _, _ = component_artifacts
        blend = load_blend_scorer(config)
        X = pd.DataFrame(
            {"f1": [0.4, -0.2, 1.0], "f2": [0.1, 0.2, -0.3]},
            index=["AAA", "BBB", "CCC"],
        )
        out = blend.score(X, ctx=object())     # ctx accepted-but-ignored
        assert isinstance(out, pd.Series)
        assert out.name == "panel_score"
        assert list(out.index) == ["AAA", "BBB", "CCC"]

    def test_missing_union_column_raises_keyerror(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        X = pd.DataFrame({"f1": [1.0, 2.0]}, index=["AAA", "BBB"])
        with pytest.raises(KeyError, match="missing columns"):
            blend.score(X)


# ── 6. metadata ──────────────────────────────────────────────────────────────

class TestMetadata:
    def test_component_identities_carried(self, component_artifacts):
        config, sha_prod, sha_clf = component_artifacts
        blend = load_blend_scorer(config)
        comps = blend.metadata["components"]
        assert len(comps) == 2
        assert comps[0]["config_fingerprint"] == PROD_FP
        assert comps[1]["config_fingerprint"] == CLF_FP
        assert comps[0]["trained_date"] == "2026-06-21"
        assert comps[1]["trained_date"] == "2026-07-26"
        assert comps[0]["artifact_path"].endswith("prod.json")

    def test_effective_cutoff_is_older_component(self, component_artifacts):
        config, _, _ = component_artifacts
        blend = load_blend_scorer(config)
        # prod trained 2026-06-21 (older), clf 2026-07-26 → conservative pick
        assert blend.metadata["effective_train_cutoff_date"] == "2026-06-21"

    def test_effective_cutoff_none_when_leg_unstamped(self):
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0, trained="2026-06-21"),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1, trained=None),
        ])
        assert blend.metadata["effective_train_cutoff_date"] is None

    def test_composite_fp_recipe_and_determinism(self, component_artifacts):
        config, _, _ = component_artifacts
        expected = "sha256:" + hashlib.sha256(
            (PROD_FP + "\n" + CLF_FP).encode("utf-8")).hexdigest()
        assert load_blend_scorer(config).metadata["config_fingerprint"] == expected
        # deterministic across loads
        assert load_blend_scorer(config).metadata["config_fingerprint"] == expected
        # order-sensitive: swapping components changes the identity
        config["ranking"]["panel_scoring"]["components"].reverse()
        swapped = load_blend_scorer(config).metadata["config_fingerprint"]
        assert swapped == "sha256:" + hashlib.sha256(
            (CLF_FP + "\n" + PROD_FP).encode("utf-8")).hexdigest()
        assert swapped != expected
        assert composite_config_fingerprint([PROD_FP, CLF_FP]) == expected


# ── 7. kernel wiring ─────────────────────────────────────────────────────────

def _kernel_ctx(config: dict, tickers=("AAA", "BBB", "CCC")) -> SimpleNamespace:
    candidates = [
        SimpleNamespace(ticker=t, rank_score=None, panel_score=None,
                        model_type=None, legacy_model_type=None)
        for t in tickers
    ]
    return SimpleNamespace(config=config, candidates=candidates, holdings={},
                           _panel_matrix=None)


def _stamped_blend_config(tmp_path) -> dict:
    """Blend config whose component-0 fp equals the LIVE config fingerprint,
    so the strict config-consistency gate passes exactly as it does for the
    production artifact today. Component paths are strategy_dir-relative."""
    from renquant_common.config_consistency import fingerprint_config

    config = {
        "watchlist": ["AAA", "BBB", "CCC"],
        "ranking": {"panel_scoring": {"enabled": True, "kind": "blend"}},
        "_strategy_dir": str(tmp_path),
    }
    live_fp = fingerprint_config(config)
    sha_prod = _write_component(
        tmp_path / "prod.json", fp=live_fp, trained_date="2026-06-21", seed=7)
    sha_clf = _write_component(
        tmp_path / "clf.json", fp=CLF_FP, trained_date="2026-07-26", seed=99)
    config["ranking"]["panel_scoring"]["components"] = [
        {"artifact_path": "prod.json",
         "expected_content_sha256": "sha256:" + sha_prod[:16],
         "expected_config_fingerprint": live_fp},
        {"artifact_path": "clf.json",
         "expected_content_sha256": "sha256:" + sha_clf[:16],
         "expected_config_fingerprint": CLF_FP},
    ]
    return config


class TestKernelWiring:
    def test_load_scorer_task_dispatches_blend_without_top_level_path(
            self, tmp_path):
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            LoadScorerTask,
        )

        ctx = _kernel_ctx(_stamped_blend_config(tmp_path))
        rc = LoadScorerTask().run(ctx)
        assert rc is not False, (
            "LoadScorerTask fail-closed on a valid blend config: "
            f"{getattr(ctx, '_panel_scoring_fail_reason', None)}")
        assert isinstance(ctx._panel_scorer, BlendPanelScorer)
        # active-scorer stamp: kind=blend, path anchored on component 0
        assert ctx._active_panel_model_type == "blend"
        assert str(ctx._active_panel_artifact_path).endswith("prod.json")

    def test_load_scorer_task_dispatches_preloaded_blend_without_top_level_path(
            self, tmp_path):
        """Regression pin for the preloaded (adapter/LEAN) branch: preloading
        a BlendPanelScorer onto ctx._panel_scorer must anchor the strict
        consistency gate + trace stamp on component 0, same as the fresh-load
        branch above — not fail closed against the composite fingerprint."""
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            LoadScorerTask,
        )

        config = _stamped_blend_config(tmp_path)
        ctx = _kernel_ctx(config)
        ctx._panel_scorer = load_blend_scorer(config)
        rc = LoadScorerTask().run(ctx)
        assert rc is not False, (
            "LoadScorerTask fail-closed on a preloaded valid blend scorer: "
            f"{getattr(ctx, '_panel_scoring_fail_reason', None)}")
        assert ctx._active_panel_model_type == "blend"
        assert str(ctx._active_panel_artifact_path).endswith("prod.json")

    def test_load_scorer_task_fails_closed_on_pin_mismatch(self, tmp_path):
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            LoadScorerTask,
        )

        config = _stamped_blend_config(tmp_path)
        config["ranking"]["panel_scoring"]["components"][1][
            "expected_content_sha256"] = "sha256:" + "0" * 16
        ctx = _kernel_ctx(config)
        assert LoadScorerTask().run(ctx) is False
        assert ctx.skip_buys is True
        assert ctx.candidates == []
        assert set(ctx._blocked_by_ticker.values()) == {"panel_scorer_load_failed"}

    def test_default_kind_missing_artifact_path_unchanged(self):
        """kind != blend regression pin: the no-artifact_path fail-close is
        byte-identical to the pre-blend behavior."""
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            LoadScorerTask,
        )

        ctx = _kernel_ctx({
            "ranking": {"panel_scoring": {"enabled": True, "kind": "xgb"}},
            "_strategy_dir": None,
        })
        assert LoadScorerTask().run(ctx) is False
        assert set(ctx._blocked_by_ticker.values()) == {
            "panel_scorer_missing_artifact_path"}

    def test_apply_scores_routes_blend_through_alpha158_raw(self, monkeypatch):
        """ApplyScoresTask routes kind=blend down the alpha158-rebuild branch
        and passes the RAW union matrix (no outer transform): candidates get
        the hand-computed z-sum of the RAW per-ticker feature values."""
        from renquant_pipeline.kernel.panel_pipeline import alpha158_features
        from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
            ApplyScoresTask,
        )

        raw_by_ticker = {
            "AAA": {"f1": 1.0, "f2": 1.0},
            "BBB": {"f1": 2.0, "f2": 1.0},
            "CCC": {"f1": 3.0, "f2": 2.0},
            "DDD": {"f1": 4.0, "f2": 4.0},
        }

        def fake_alpha158(ohlcv, today):  # noqa: ARG001
            marker = float(ohlcv["close"].iloc[-1])
            return dict(raw_by_ticker[f"{'ABCD'[int(marker)]*3}"])

        monkeypatch.setattr(alpha158_features, "compute_alpha158_at",
                            fake_alpha158)

        tickers = ["AAA", "BBB", "CCC", "DDD"]
        blend = BlendPanelScorer([
            _fake_component(_FakeScorer(FEAT_COLS, "f1"), 0),
            _fake_component(_FakeScorer(FEAT_COLS, "f2"), 1),
        ])
        ctx = _kernel_ctx(
            {"ranking": {"panel_scoring": {"enabled": True, "kind": "blend"}},
             "_strategy_dir": None},
            tickers=tickers,
        )
        ctx._panel_scorer = blend
        ctx._active_panel_model_type = "blend"
        ctx._panel_matrix = pd.DataFrame(
            {"__alpha158_target__": 1.0}, index=tickers)
        ctx.today = datetime.date(2026, 7, 27)
        ctx.ohlcv = {
            t: pd.DataFrame({"close": [float(i)] * 80})
            for i, t in enumerate(tickers)
        }

        ApplyScoresTask().run(ctx)

        expected = _z(np.array([1.0, 2.0, 3.0, 4.0])) + \
            _z(np.array([1.0, 1.0, 2.0, 4.0]))
        got = {c.ticker: c.panel_score for c in ctx.candidates}
        assert len(got) == 4, (
            f"blend did not score all candidates: {got}; blocked="
            f"{getattr(ctx, '_blocked_by_ticker', None)}")
        np.testing.assert_allclose(
            [got[t] for t in tickers], expected, rtol=1e-12)
        # rank_score overwritten in lockstep (funnel consumes rank_score)
        np.testing.assert_allclose(
            [c.rank_score for c in ctx.candidates], expected, rtol=1e-12)
        # the RAW union matrix (not a transformed copy) is what got stamped
        assert sorted(ctx._panel_matrix.columns) == FEAT_COLS
        assert float(ctx._panel_matrix.loc["DDD", "f1"]) == 4.0


# ── 8. blend-lane broker tag (umbrella#535 mirror) ───────────────────────────

class TestBlendBrokerTag:
    """``alpaca_shadow_blend`` accepted by the broker allowlist in BOTH
    state_paths copies (test_shadow_arm_broker_tags conventions), with its
    own isolated state file and no collision with the other shadow tags."""

    TAG = "alpaca_shadow_blend"

    @pytest.mark.parametrize("copy_name", ["top", "kernel"])
    def test_tag_accepted_in_both_copies(self, copy_name, tmp_path):
        from renquant_pipeline import state_paths as top_state_paths
        from renquant_pipeline.kernel import state_paths as kernel_state_paths

        mod = {"top": top_state_paths, "kernel": kernel_state_paths}[copy_name]
        assert self.TAG in mod.ALLOWED_BROKERS
        assert mod.live_state_path(tmp_path, self.TAG).name == (
            f"live_state.{self.TAG}.json")

    def test_tag_disjoint_from_other_shadow_tags(self, tmp_path):
        from renquant_pipeline import state_paths as mod

        tags = ("alpaca_shadow", "alpaca_shadow_a", "alpaca_shadow_b", self.TAG)
        assert len({mod.live_state_path(tmp_path, t) for t in tags}) == 4

    def test_unknown_tag_still_rejected(self, tmp_path):
        from renquant_pipeline import state_paths as mod

        with pytest.raises(ValueError, match="Unknown broker_name"):
            mod.live_state_path(tmp_path, "alpaca_shadow_blend2")
