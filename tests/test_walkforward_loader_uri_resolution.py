"""Regression test for WalkForwardModelLoader relative-URI resolution.

AUDIT REGRESSION GUARD — 2026-06-02 sim crash.

Bug: ``WalkForwardModelLoader.model_as_of`` passed ``chosen.artifact_uri``
directly to ``PanelScorer.load``. When the manifest stored a relative URI
(e.g. ``artifacts/walkforward_v2_20260602/2024-01-01/panel-ltr.json``),
``PanelScorer.load`` resolved it against the process cwd, which during
a WF-gate sim is NOT the strategy dir → ``FileNotFoundError`` after
~12 min into each WF cut.

Fix: route through ``_resolve_uri`` so relative URIs are anchored to the
manifest's parent directory (matching the contract already used by
``calibrator_as_of`` and ``_scorer_fingerprints_for_entry``).

The companion test in the umbrella suite is
``tests/test_walkforward_loader.py::TestRelativeArtifactUriResolution``.
"""
from __future__ import annotations

import json
from pathlib import Path

from renquant_pipeline.kernel.walk_forward.loader import WalkForwardModelLoader


def _make_manifest(tmp_path, rows):
    p = tmp_path / "walkforward_manifest.json"
    p.write_text(json.dumps({
        "cadence_days": 21,
        "training_window_years": 3.0,
        "retrains": rows,
    }))
    return p


def _row(cutoff, trained, uri):
    return {
        "cutoff_date": cutoff,
        "trained_date": trained,
        "artifact_uri": uri,
    }


def test_relative_artifact_uri_resolved_against_manifest_parent(
    tmp_path, monkeypatch,
):
    rel = "artifacts/walkforward_v2/2024-01-01/panel-ltr.json"
    abs_path = tmp_path / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("{}")

    rows = [_row("2024-01-01T00:00:00", "2024-01-02T03:00:00", rel)]
    manifest = _make_manifest(tmp_path, rows)

    captured: list[str] = []

    class _FakeScorer:
        def __init__(self, uri):
            self.uri = uri

    def fake_load(path):
        captured.append(str(path))
        assert Path(str(path)).is_absolute(), (
            f"PanelScorer.load received non-absolute path {path!r}; "
            "loader must resolve relative artifact_uri against the "
            "manifest folder before delegating."
        )
        return _FakeScorer(str(path))

    from renquant_pipeline.kernel.panel_pipeline import panel_scorer as _ps
    monkeypatch.setattr(_ps.PanelScorer, "load", staticmethod(fake_load))

    monkeypatch.chdir(tmp_path.parent)
    loader = WalkForwardModelLoader(manifest)
    loader.model_as_of("2024-01-15")
    assert captured == [str(abs_path)]


def test_strategy_dir_relative_uri_falls_back_to_ancestor(tmp_path, monkeypatch):
    """GBDT-corpus regression (broke weekly_wf_promote since 2026-05-24).

    A manifest under ``artifacts/sim/`` whose rows store strategy-dir-relative
    URIs (``artifacts/walkforward_gbdt/...``) must resolve to the real file, not
    the doubled ``artifacts/sim/artifacts/...`` path the manifest-parent anchor
    produces.
    """
    manifest_dir = tmp_path / "artifacts" / "sim"
    manifest_dir.mkdir(parents=True)
    rel = "artifacts/walkforward_gbdt/2024-01-01/panel-ltr.json"
    # Real artifact lives strategy-dir-relative (tmp_path), NOT under the manifest folder.
    real = tmp_path / rel
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("{}")
    # The doubled manifest-parent path must NOT exist — that is the bug.
    assert not (manifest_dir / rel).exists()

    manifest = manifest_dir / "walkforward_manifest_gbdt.json"
    manifest.write_text(json.dumps({
        "cadence_days": 21,
        "training_window_years": 3.0,
        "retrains": [_row("2024-01-01T00:00:00", "2024-01-02T03:00:00", rel)],
    }))

    captured: list[str] = []

    def fake_load(path):
        captured.append(str(path))
        return object()

    from renquant_pipeline.kernel.panel_pipeline import panel_scorer as _ps
    monkeypatch.setattr(_ps.PanelScorer, "load", staticmethod(fake_load))

    loader = WalkForwardModelLoader(manifest)
    loader.model_as_of("2024-01-15")
    assert captured == [str(real)]


def test_absolute_artifact_uri_still_works(tmp_path, monkeypatch):
    abs_path = tmp_path / "panel-ltr.json"
    abs_path.write_text("{}")
    rows = [_row("2024-01-01T00:00:00", "2024-01-02T03:00:00", str(abs_path))]
    manifest = _make_manifest(tmp_path, rows)

    captured: list[str] = []

    def fake_load(path):
        captured.append(str(path))
        return object()

    from renquant_pipeline.kernel.panel_pipeline import panel_scorer as _ps
    monkeypatch.setattr(_ps.PanelScorer, "load", staticmethod(fake_load))

    loader = WalkForwardModelLoader(manifest)
    loader.model_as_of("2024-01-15")
    assert captured == [str(abs_path)]
