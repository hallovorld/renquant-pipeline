"""Tests for the as-served feature-snapshot writer.

The load-bearing test here is `test_payload_satisfies_the_consumer_contract`:
the whole point of this module is that the file it emits is accepted by
``renquant_orchestrator.realtime_data_plane.FeatureSnapshot.from_mapping``. That
class lives in another repo, so its four validation rules are restated here
verbatim from its source rather than imported — an import would couple this
suite to a sibling checkout and skip silently when absent, which is exactly how
a contract test stops testing anything.

Rules, quoted from ``FeatureSnapshot.from_mapping`` (Codex #221):
  * payload must be a Mapping
  * ``feature_cutoff``          non-empty after str().strip()
  * ``feature_builder_version`` non-empty after str().strip()
  * ``features``               a Mapping AND non-empty; keys upper-cased
"""
from __future__ import annotations

import json
import math
import os

import pandas as pd
import pytest

from renquant_pipeline.kernel.panel_pipeline.feature_snapshot_writer import (
    ENV_DIR,
    builder_version,
    matrix_to_features,
    persist_from_context,
    resolve_output_dir,
    write_snapshot,
)


class _Scorer:
    def __init__(self, cols):
        self.feature_cols = list(cols)


class _Ctx:
    """Minimal stand-in for InferenceContext's read surface."""

    def __init__(self, matrix, cols=("a", "b"), cutoff="2026-08-05", config=None,
                 session_date="2026-08-06"):
        self._panel_matrix = matrix
        self._panel_scorer = _Scorer(cols)
        self._fm_inputs = {"today_ts": cutoff}
        self.config = config if config is not None else {}
        self.session_date = session_date


def _matrix():
    return pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]}, index=["aapl", "MSFT"])


# ── enablement ─────────────────────────────────────────────────────────────

def test_disabled_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_DIR, raising=False)
    assert resolve_output_dir({}) is None
    assert persist_from_context(_Ctx(_matrix())) is None
    assert list(tmp_path.iterdir()) == []


def test_env_var_enables_when_config_absent(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    assert resolve_output_dir({}) == str(tmp_path)


def test_config_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path / "from_env"))
    cfg = {"ranking": {"panel_scoring": {"feature_snapshot_dir": str(tmp_path / "from_cfg")}}}
    assert resolve_output_dir(cfg) == str(tmp_path / "from_cfg")


def test_blank_config_value_falls_through_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    cfg = {"ranking": {"panel_scoring": {"feature_snapshot_dir": "   "}}}
    assert resolve_output_dir(cfg) == str(tmp_path)


def test_malformed_config_does_not_raise(monkeypatch):
    monkeypatch.delenv(ENV_DIR, raising=False)
    for bad in (None, [], "nope", {"ranking": "not-a-dict"}):
        assert resolve_output_dir(bad) is None


# ── the consumer contract ──────────────────────────────────────────────────

def test_payload_satisfies_the_consumer_contract(tmp_path):
    path = write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a", "b"], _matrix())
    payload = json.loads(open(path).read())

    assert isinstance(payload, dict)
    assert str(payload.get("feature_cutoff", "")).strip()
    assert str(payload.get("feature_builder_version", "")).strip()
    feats = payload.get("features")
    assert isinstance(feats, dict) and feats

    # The consumer upper-cases keys; emitting them already upper-cased keeps the
    # digest it derives stable against a lower-case source index.
    assert set(feats) == {"AAPL", "MSFT"}
    assert feats["AAPL"] == {"a": 1.0, "b": 3.0}


def test_filename_is_what_the_wrapper_looks_for(tmp_path):
    # run_shadow_serving.sh probes data/rq105/feature_snapshot_$TS.json
    path = write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"], _matrix())
    assert os.path.basename(path) == "feature_snapshot_2026-08-06.json"


def test_no_digest_is_written(tmp_path):
    # The consumer derives the digest itself; a second one written here could
    # drift from it, and the mismatch would be silent.
    path = write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"], _matrix())
    assert "digest" not in json.loads(open(path).read())


# ── builder identity ───────────────────────────────────────────────────────

def test_builder_version_tracks_the_column_set():
    assert builder_version(["a", "b"]) != builder_version(["a", "c"])


def test_builder_version_is_order_sensitive():
    # The matrix is positional at score time, so a reordering is a different build.
    assert builder_version(["a", "b"]) != builder_version(["b", "a"])


def test_builder_version_names_the_builder():
    assert "renquant_pipeline.kernel.panel_pipeline.feature_matrix" in builder_version(["a"])


# ── value handling ─────────────────────────────────────────────────────────

def test_nan_becomes_null_not_dropped():
    # A NaN feature is information — the coverage gate tolerates some. Dropping
    # it would make an incomplete row look complete.
    out = matrix_to_features(pd.DataFrame({"a": [math.nan]}, index=["X"]))
    assert out == {"X": {"a": None}}


def test_infinity_becomes_null():
    out = matrix_to_features(pd.DataFrame({"a": [math.inf]}, index=["X"]))
    assert out == {"X": {"a": None}}


def test_accepts_a_plain_dict_matrix():
    # panel_scoring.py builds a dict rather than a DataFrame; a writer that only
    # understood one shape would emit nothing for the other.
    assert matrix_to_features({"aapl": {"a": 1.0}}) == {"AAPL": {"a": 1.0}}


def test_unusable_matrix_types_yield_no_rows():
    for bad in (None, 42, "frame", ["AAPL"]):
        assert matrix_to_features(bad) == {}


# ── refusals ───────────────────────────────────────────────────────────────

def test_empty_matrix_writes_no_file(tmp_path):
    assert write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"], pd.DataFrame()) is None
    assert list(tmp_path.iterdir()) == []


def test_missing_cutoff_refuses_rather_than_substituting_today(tmp_path, monkeypatch):
    # Stamping an unverified as-of onto real feature values corrupts every
    # downstream provenance check; an absent snapshot is recoverable.
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    ctx = _Ctx(_matrix())
    ctx._fm_inputs = {"today_ts": ""}
    assert persist_from_context(ctx) is None
    assert list(tmp_path.iterdir()) == []


def test_gated_none_matrix_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    assert persist_from_context(_Ctx(None)) is None
    assert list(tmp_path.iterdir()) == []


# ── fail-open ──────────────────────────────────────────────────────────────

def test_unwritable_destination_does_not_raise(tmp_path, monkeypatch):
    # This runs inside the live order-placing pipeline. A snapshot failure must
    # never become a trading outage.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    monkeypatch.setenv(ENV_DIR, str(blocker / "under_a_file"))
    assert persist_from_context(_Ctx(_matrix())) is None


def test_context_missing_every_attribute_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))

    class Bare:
        pass

    assert persist_from_context(Bare()) is None


def test_scorer_absent_refuses_to_write(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    ctx = _Ctx(_matrix())
    ctx._panel_scorer = None
    # Without a scorer, feature_cols would degrade to [] and the written
    # builder_version would misrepresent the matrix's real columns — refuse
    # rather than write a mislabelled snapshot (same policy as a missing cutoff).
    assert persist_from_context(ctx) is None
    assert list(tmp_path.iterdir()) == []


# ── atomicity ──────────────────────────────────────────────────────────────

def test_no_temp_file_survives_a_successful_write(tmp_path):
    write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"], _matrix())
    assert [p.name for p in tmp_path.iterdir()] == ["feature_snapshot_2026-08-06.json"]


def test_rewrite_replaces_cleanly(tmp_path):
    write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"], _matrix())
    p2 = write_snapshot(str(tmp_path), "2026-08-06", "2026-08-05", ["a"],
                        pd.DataFrame({"a": [9.0]}, index=["NVDA"]))
    assert json.loads(open(p2).read())["features"] == {"NVDA": {"a": 9.0}}
    assert len(list(tmp_path.iterdir())) == 1


# ── wiring ─────────────────────────────────────────────────────────────────

def test_task_runs_last_so_it_sees_the_post_gate_matrix():
    # Persisting the pre-gate matrix would emit rows that were never scored.
    from renquant_pipeline.kernel.panel_pipeline.tasks_feature_matrix import (
        BuildFeatureMatrixJob,
    )
    names = [t.name for t in BuildFeatureMatrixJob().tasks]
    assert names[-1] == "PersistFeatureSnapshotTask"
    assert names.index("DriftGuardTask") < names.index("PersistFeatureSnapshotTask")


def test_task_is_advisory_and_never_fails_the_job(tmp_path, monkeypatch):
    from renquant_pipeline.kernel.panel_pipeline.tasks_feature_matrix import (
        PersistFeatureSnapshotTask,
    )
    monkeypatch.delenv(ENV_DIR, raising=False)

    class Bare:
        pass

    # A Task returning False would abort the chain; this one must not.
    assert PersistFeatureSnapshotTask().run(Bare()) is None


@pytest.mark.parametrize("cutoff", ["2026-08-05", "2026-08-05T20:00:00-04:00"])
def test_cutoff_is_passed_through_verbatim(tmp_path, monkeypatch, cutoff):
    monkeypatch.setenv(ENV_DIR, str(tmp_path))
    path = persist_from_context(_Ctx(_matrix(), cutoff=cutoff))
    assert json.loads(open(path).read())["feature_cutoff"] == cutoff
