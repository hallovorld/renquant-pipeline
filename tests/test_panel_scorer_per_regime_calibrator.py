"""Track A (2026-06-02) — tests for the explicit `calibrator_per_regime`
schema on `ranking.panel_scoring`.

Subrepo mirror of `tests/test_panel_scorer_per_regime_calibrator.py` in the
umbrella RenQuant repo. Asserts the same invariants on the
`renquant_pipeline.kernel.panel_pipeline.job_panel_scoring.LoadGlobalCalibrationTask`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    LoadGlobalCalibrationTask,
)


def _write_calibrator(path: Path, regime: str | None = None) -> None:
    metadata = {"n_rows": 500}
    if regime is not None:
        metadata["regime"] = regime
        metadata["fit_window_regime"] = regime
    cal = GlobalPanelCalibration(
        prob_x=np.array([-1.0, 1.0]),
        prob_y=np.array([0.1, 0.9]),
        er_x=np.array([-1.0, 1.0]),
        er_y=np.array([-0.01, 0.01]),
        metadata=metadata,
    )
    cal.save(path)


def _make_ctx(
    strategy_dir: Path,
    *,
    regime: str | None,
    per_regime_cfg: dict | None,
    pooled_relpath: str = "artifacts/panel-rank-calibration.json",
) -> SimpleNamespace:
    panel_scoring: dict = {
        "global_calibration": {
            "enabled": True,
            "artifact_path": pooled_relpath,
            "regime_conditional": {
                "enabled": False,
                "artifact_pattern":
                    "artifacts/panel-calibration-{regime}.json",
                "regimes": ["BULL_CALM", "BEAR"],
            },
            "strict_scorer_match": False,
        },
    }
    if per_regime_cfg is not None:
        panel_scoring["calibrator_per_regime"] = per_regime_cfg
    return SimpleNamespace(
        config={
            "_strategy_dir": str(strategy_dir),
            "ranking": {"panel_scoring": panel_scoring},
        },
        regime=regime,
        candidates=[],
        holdings={},
    )


def test_no_per_regime_field_loads_pooled_only(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")

    ctx = _make_ctx(tmp_path, regime="BULL_CALM", per_regime_cfg=None)
    LoadGlobalCalibrationTask().run(ctx)

    assert ctx._global_calibrator is not None
    assert getattr(ctx, "_regime_calibrators", None) in (None, {})


def test_all_four_regimes_loaded(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")
    per_regime_paths = {}
    for regime in ("BULL_CALM", "BULL_VOLATILE", "BEAR", "CHOPPY"):
        p = art_dir / f"panel-rank-calibration.{regime.lower()}.json"
        _write_calibrator(p, regime=regime)
        per_regime_paths[regime] = str(
            p.relative_to(tmp_path).as_posix()
        )

    ctx = _make_ctx(
        tmp_path, regime="BULL_CALM", per_regime_cfg=per_regime_paths,
    )
    LoadGlobalCalibrationTask().run(ctx)

    loaded = ctx._regime_calibrators
    assert set(loaded.keys()) == {
        "BULL_CALM", "BULL_VOLATILE", "BEAR", "CHOPPY",
    }
    for regime, cal in loaded.items():
        assert cal.metadata.get("regime") == regime


def test_partial_map_falls_back_for_unlisted(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")
    bc = art_dir / "panel-rank-calibration.bull_calm.json"
    _write_calibrator(bc, regime="BULL_CALM")

    per_regime = {"BULL_CALM": str(bc.relative_to(tmp_path).as_posix())}

    ctx_bc = _make_ctx(tmp_path, regime="BULL_CALM", per_regime_cfg=per_regime)
    LoadGlobalCalibrationTask().run(ctx_bc)
    assert set(ctx_bc._regime_calibrators.keys()) == {"BULL_CALM"}
    picked = (
        ctx_bc._regime_calibrators.get(ctx_bc.regime)
        or ctx_bc._global_calibrator
    )
    assert picked is ctx_bc._regime_calibrators["BULL_CALM"]

    ctx_bear = _make_ctx(tmp_path, regime="BEAR", per_regime_cfg=per_regime)
    LoadGlobalCalibrationTask().run(ctx_bear)
    assert "BEAR" not in ctx_bear._regime_calibrators
    picked_bear = (
        ctx_bear._regime_calibrators.get(ctx_bear.regime)
        or ctx_bear._global_calibrator
    )
    assert picked_bear is ctx_bear._global_calibrator


def test_missing_file_raises(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")

    per_regime = {"BULL_CALM": "artifacts/does-not-exist.json"}
    ctx = _make_ctx(tmp_path, regime="BULL_CALM", per_regime_cfg=per_regime)
    with pytest.raises(FileNotFoundError, match="calibrator_per_regime"):
        LoadGlobalCalibrationTask().run(ctx)


def test_invalid_regime_name_raises(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")
    ok_path = art_dir / "panel-rank-calibration.bull_calm.json"
    _write_calibrator(ok_path, regime="BULL_CALM")

    per_regime = {
        "BULL_CALM": str(ok_path.relative_to(tmp_path).as_posix()),
        "MYSTERY_REGIME": "artifacts/whatever.json",
    }
    ctx = _make_ctx(tmp_path, regime="BULL_CALM", per_regime_cfg=per_regime)
    with pytest.raises(ValueError, match="invalid regime keys"):
        LoadGlobalCalibrationTask().run(ctx)


def test_non_dict_value_raises(tmp_path):
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    _write_calibrator(art_dir / "panel-rank-calibration.json")

    ctx = _make_ctx(
        tmp_path, regime="BULL_CALM",
        per_regime_cfg=["BULL_CALM=foo.json"],  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="must be a dict"):
        LoadGlobalCalibrationTask().run(ctx)
