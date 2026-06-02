from __future__ import annotations

import json

import numpy as np
import pytest

from renquant_pipeline.kernel.panel_pipeline.global_calibrator import GlobalPanelCalibration
from renquant_pipeline.kernel.panel_pipeline.panel_scorer import model_content_sha256
from renquant_pipeline.kernel.walk_forward.loader import WalkForwardModelLoader


def test_runtime_calibrator_interpolates_and_scales_horizon() -> None:
    cal = GlobalPanelCalibration(
        prob_x=np.array([0.0, 1.0, 2.0]),
        prob_y=np.array([0.2, 0.5, 0.8]),
        er_x=np.array([0.0, 1.0, 2.0]),
        er_y=np.array([-0.06, 0.0, 0.06]),
        metadata={"lookahead_days_used": 60},
    )

    assert cal.calibrate_probability(0.5) == pytest.approx(0.35)
    np.testing.assert_allclose(
        cal.calibrate_probability_vec(np.array([0.0, 0.5, 2.0])),
        [0.2, 0.35, 0.8],
    )
    assert cal.expected_return(2.0, horizon_days=30) == pytest.approx(0.03)


def test_runtime_calibrator_load_clips_out_of_range_values(tmp_path) -> None:
    artifact = tmp_path / "cal.json"
    artifact.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "global_panel_calibration",
                "probability": {"x": [0.0, 1.0], "y": [-0.2, 1.2]},
                "expected_return": {"x": [0.0, 1.0], "y": [-0.5, 0.5]},
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    cal = GlobalPanelCalibration.load(artifact)

    np.testing.assert_allclose(cal.prob_y, [0.0, 1.0])
    np.testing.assert_allclose(cal.er_y, [-0.2, 0.2])


def test_runtime_calibrator_empty_knots_degrade_to_neutral() -> None:
    cal = GlobalPanelCalibration([], [], [], [])

    assert cal.calibrate_probability(123.0) == pytest.approx(0.5)
    assert cal.expected_return(123.0) == pytest.approx(0.0)
    np.testing.assert_allclose(cal.calibrate_probability_vec(np.array([1.0, 2.0])), [0.5, 0.5])
    np.testing.assert_allclose(cal.expected_return_vec(np.array([1.0, 2.0])), [0.0, 0.0])


def test_walkforward_loader_loads_pipeline_calibrator_with_fingerprint(tmp_path) -> None:
    scorer_payload = {
        "version": 1,
        "feature_cols": ["feature_a"],
        "booster_raw_json": "{\"learner\":{}}",
        "metadata": {"operator_note": "mutable"},
    }
    scorer = tmp_path / "scorer.json"
    scorer.write_text(json.dumps(scorer_payload), encoding="utf-8")
    scorer_fp = model_content_sha256(scorer_payload)

    cal = GlobalPanelCalibration(
        [0.0, 1.0],
        [0.4, 0.7],
        [0.0, 1.0],
        [0.01, 0.03],
        metadata={"scorer_model_content_fingerprint": scorer_fp},
    )
    cal_path = tmp_path / "calibrator.json"
    cal.save(cal_path)

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "retrains": [
                    {
                        "cutoff_date": "2024-01-02",
                        "trained_date": "2024-01-03",
                        "artifact_uri": "scorer.json",
                        "calibrator_uri": "calibrator.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    loaded = WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")

    assert isinstance(loaded, GlobalPanelCalibration)
    assert loaded.calibrate_probability(1.0) == pytest.approx(0.7)
