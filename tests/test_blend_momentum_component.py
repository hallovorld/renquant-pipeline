"""pipeline#260 (GOAL-8 S1): the ``kind: momentum_residual`` blend component.

The refusal paths that fire before any momentum loading are CI-covered in
``tests/test_blend_scorer.py``; this file exercises the REAL ledger-chain
loading path end-to-end, so it importorskips the model distribution exactly
like ``tests/test_momentum_residual_shadow_handler.py`` (skipped on hosted
CI, runs wherever the sibling checkout provides ``renquant_model_momentum``).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

mm = pytest.importorskip(
    "renquant_model_momentum",
    reason="renquant-model distribution not on the path (CI has no model "
           "checkout; locally the sibling checkout provides it)")
pytest.importorskip("renquant_model_common.momentum_features")

xgb = pytest.importorskip("xgboost")

from renquant_pipeline.kernel.panel_pipeline.blend_scorer import (  # noqa: E402
    composite_config_fingerprint,
    load_blend_scorer,
)
from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (  # noqa: E402
    MOMENTUM_DATED_ARTIFACT_BASENAME,
    ShadowNotYetPublished,
)

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
CUTOFF = "2026-07-31"
FEAT_COLS = ["f1", "f2"]
PROD_FP = "sha256:f8fb2259b2bf1537"

# v0-domain-valid params, mirroring test_momentum_residual_shadow_handler.
PARAMS = {
    "params_version": "v0", "window": 60, "skip": 5, "min_obs": 30,
    "min_features": 2, "names_per_date_floor": 3, "min_side_obs": 5,
}


class _SyntheticReaders:
    def __init__(self, universe, asof, *, n_days=90, seed=7):
        idx = pd.bdate_range(end=pd.Timestamp(asof), periods=n_days)
        rng = np.random.default_rng(seed)
        self._returns = {
            t: pd.Series(rng.normal(0.0005 * (i + 1), 0.02, n_days), index=idx)
            for i, t in enumerate([*universe, "SPY"])}
        self._volume = {
            t: pd.Series(rng.integers(1_000, 100_000, n_days).astype(float),
                         index=idx)
            for t in universe}
        self._sectors = {t: ("TECH" if i % 2 else "ENER")
                         for i, t in enumerate(universe)}

    def tr_returns(self, ticker):
        return self._returns.get(ticker)

    def volume(self, ticker):
        return self._volume.get(ticker)

    def market_tr_returns(self):
        return self._returns["SPY"]

    def sector_of(self):
        return dict(self._sectors)

    def read_digests(self):
        return {"synthetic": "0" * 64}


def _publish(root: Path, asof: str, *, seed=7) -> dict:
    artifact = mm.train_momentum_artifact(
        asof, UNIVERSE, PARAMS,
        readers=_SyntheticReaders(UNIVERSE, asof, seed=seed))
    dated = root / artifact["cutoff_date"] / MOMENTUM_DATED_ARTIFACT_BASENAME
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(
        json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8")
    mm.append_to_artifact_ledger(artifact, root / "momentum_artifact_ledger.jsonl")
    return artifact


def _train_booster(seed: int):
    dtrain = xgb.DMatrix(
        [[1.0, 0.2], [0.8, 0.1], [-1.0, 0.0], [-0.7, -0.1]],
        label=[1.0, 0.8, -1.0, -0.8])
    return xgb.train(
        {"objective": "reg:squarederror", "max_depth": 2, "eta": 0.7,
         "nthread": 1, "verbosity": 0, "seed": seed},
        dtrain, num_boost_round=6, verbose_eval=False)


def _write_panel_component(path: Path) -> str:
    booster = _train_booster(7)
    payload = {
        "version": 2,
        "kind": "panel_ltr_xgboost",
        "feature_cols": list(FEAT_COLS),
        "config_fingerprint": PROD_FP,
        "trained_date": "2026-06-21",
        "booster_raw_json": bytes(
            booster.save_raw(raw_format="json")).decode("utf-8"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _momentum_fp(artifact: dict) -> str:
    canon = json.dumps(dict(artifact["params"]), sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
    version = artifact["params"]["params_version"]
    return f"momentum-{version}-{hashlib.sha256(canon.encode()).hexdigest()[:16]}"


def _blend_config(tmp_path: Path, momentum_fp: str) -> dict:
    sha_prod = _write_panel_component(tmp_path / "prod.json")
    return {
        "ranking": {"panel_scoring": {
            "enabled": True,
            "kind": "blend",
            "components": [
                {"artifact_path": str(tmp_path / "prod.json"),
                 "expected_content_sha256": "sha256:" + sha_prod[:16],
                 "expected_config_fingerprint": PROD_FP},
                {"kind": "momentum_residual",
                 "artifact_path": str(
                     tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"),
                 "expected_config_fingerprint": momentum_fp},
            ],
        }},
        "_strategy_dir": None,
    }


def test_momentum_component_happy_path_loads_and_scores(tmp_path):
    artifact = _publish(tmp_path / "momentum", CUTOFF)
    fp = _momentum_fp(artifact)
    scorer = load_blend_scorer(_blend_config(tmp_path, fp))

    comp = scorer.components[1]
    assert comp.config_fingerprint == fp
    assert comp.content_sha256 == artifact["content_sha256"]
    assert comp.effective_train_cutoff_date == CUTOFF
    # Union feature cols = the panel leg's only (momentum is matrix-less).
    assert scorer.feature_cols == sorted(FEAT_COLS)
    # Composite fp = the documented recipe over BOTH stored fps verbatim.
    assert scorer.metadata["config_fingerprint"] == \
        composite_config_fingerprint([PROD_FP, fp])

    # Score over panel features + one name outside the momentum universe:
    # intersection semantics — the outside name's total is NaN (NaN
    # propagates through the sum), scored names are finite.
    idx = [*UNIVERSE, "GGG"]
    matrix = pd.DataFrame(
        {"f1": np.linspace(-1, 1, len(idx)), "f2": np.linspace(1, -1, len(idx))},
        index=idx)
    out = scorer.score(matrix)
    scored_momentum = {t for t, v in artifact["scores"].items()
                       if isinstance(v, (int, float))
                       and not isinstance(v, bool) and np.isfinite(v)}
    assert np.isnan(out.loc["GGG"])
    finite_expected = [t for t in UNIVERSE if t in scored_momentum]
    assert finite_expected, "fixture must score at least one universe name"
    assert np.isfinite(out.loc[finite_expected]).all()


def test_momentum_component_fp_mismatch_fails_closed(tmp_path):
    _publish(tmp_path / "momentum", CUTOFF)
    config = _blend_config(tmp_path, "momentum-v0-0000000000000000")
    with pytest.raises(ValueError, match="config_fingerprint MISMATCH"):
        load_blend_scorer(config)


def test_momentum_component_tampered_chain_fails_closed(tmp_path):
    artifact = _publish(tmp_path / "momentum", CUTOFF)
    ledger = tmp_path / "momentum" / "momentum_artifact_ledger.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[0])
    row["artifact_content_sha256"] = "sha256:" + "0" * 16
    lines[0] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = _blend_config(tmp_path, _momentum_fp(artifact))
    with pytest.raises(ValueError, match="ledger_chain_verification_failed"):
        load_blend_scorer(config)


def test_momentum_component_empty_ledger_is_fail_closed_for_blend(tmp_path):
    """A chain-verified EMPTY ledger is the designed pending window for the
    SHADOW lane, but a blend PRIMARY cannot half-exist: the inner loader's
    ShadowNotYetPublished propagates out of load_blend_scorer untouched
    (LoadScorerTask maps any load raise to panel_scorer_load_failed)."""
    root = tmp_path / "momentum"
    root.mkdir(parents=True)
    (root / "momentum_artifact_ledger.jsonl").write_text("", encoding="utf-8")
    config = _blend_config(tmp_path, "momentum-v0-0000000000000000")
    with pytest.raises(ShadowNotYetPublished):
        load_blend_scorer(config)


def test_composite_fp_stable_across_weekly_publishes(tmp_path):
    """Two weekly publishes with the SAME frozen params: the artifact bytes
    (and their sha) change, the recipe fp does not — so the composite fp is
    stable, which is exactly why the byte pin is refused on this leg."""
    a1 = _publish(tmp_path / "momentum", "2026-07-24", seed=7)
    fp = _momentum_fp(a1)
    s1 = load_blend_scorer(_blend_config(tmp_path, fp))
    a2 = _publish(tmp_path / "momentum", CUTOFF, seed=99)
    s2 = load_blend_scorer(_blend_config(tmp_path, fp))
    assert a1["content_sha256"] != a2["content_sha256"]
    assert s1.components[1].content_sha256 == a1["content_sha256"]
    assert s2.components[1].content_sha256 == a2["content_sha256"]  # new tail
    assert s1.metadata["config_fingerprint"] == s2.metadata["config_fingerprint"]
