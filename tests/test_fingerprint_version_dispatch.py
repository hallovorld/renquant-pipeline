"""M6 stage-2 step-1: schema-version-dispatched fingerprint verification.

Design: renquant-orchestrator
``doc/design/2026-07-03-m6-stage2-fingerprint-migration.md`` §3 step 1 /
§6 step-1 acceptance — fixtures for all four dispatch cases at BOTH
fail-closed binding checks:

* v1/v1 match (and mismatch),
* legacy/legacy match (incl. the historical prefix acceptance),
* cross-schema never-match (the no-OR proof: a v1-stamped artifact can
  NOT be accepted via its legacy hash, in either direction),
* flag-off ``VersionGapError``-remedy fail-close on versionless stamps,

plus the unstamped current-behavior fallback, the flag-default regression
(legacy population verifies exactly as before the dispatch existed), and
the ``PanelScorer.load`` both-identities telemetry stamps.
"""
from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

import renquant_common.model_fingerprint as shared
from renquant_pipeline.kernel.panel_pipeline import fingerprint_dispatch as fd
from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _assert_calibrator_matches_scorer,
)
from renquant_pipeline.kernel.walk_forward.loader import WalkForwardModelLoader


# ---------------------------------------------------------------------------
# Fixture payloads (fully classified under the v1 tables so both schemas'
# hashes are computable — mirrors the shared frozen-vector payload).
# ---------------------------------------------------------------------------

def _payload() -> dict:
    return {
        "kind": "panel_ltr_xgboost",
        "version": 3,
        "feature_cols": ["a", "b", "c"],
        "params": {"objective": "rank:pairwise", "max_depth": 4},
        "booster_raw_json": '{"fake": "booster"}',
        "label_col": "fwd_60d_excess",
        "trained_date": "2026-06-01",
        "metadata": {"note": "irrelevant"},
    }


def _legacy_hash(payload: dict) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return shared._legacy_model_content_sha256(payload)


def _v1_hash(payload: dict) -> str:
    return shared.model_content_sha256(payload)


def _write(tmp_path: Path, payload: dict, name: str) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return p


def _legacy_stamped_payload() -> dict:
    payload = _payload()
    payload["model_content_fingerprint"] = _legacy_hash(payload)
    return payload


def _v1_stamped_payload() -> dict:
    payload = _payload()
    payload.update(shared.stamp(payload))
    return payload


def _ctx(scorer_meta: dict | None, *, accept_legacy: bool | None = None) -> SimpleNamespace:
    fp_cfg = {} if accept_legacy is None else {"accept_legacy_stamps": accept_legacy}
    return SimpleNamespace(
        _panel_scorer=(
            None if scorer_meta is None
            else SimpleNamespace(metadata=scorer_meta)
        ),
        config={"ranking": {"panel_scoring": {"fingerprint": fp_cfg}}},
    )


def _calibrator(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(metadata=meta)


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

def test_accept_legacy_stamps_defaults_true() -> None:
    assert fd.accept_legacy_stamps(None) is True
    assert fd.accept_legacy_stamps({}) is True
    assert fd.accept_legacy_stamps({"ranking": {}}) is True
    assert fd.accept_legacy_stamps("not-a-mapping") is True


def test_accept_legacy_stamps_reads_the_config_key() -> None:
    cfg = {"ranking": {"panel_scoring": {"fingerprint": {"accept_legacy_stamps": False}}}}
    assert fd.accept_legacy_stamps(cfg) is False
    cfg["ranking"]["panel_scoring"]["fingerprint"]["accept_legacy_stamps"] = True
    assert fd.accept_legacy_stamps(cfg) is True


# ---------------------------------------------------------------------------
# Daily-path binding check (_assert_calibrator_matches_scorer, site 2)
# ---------------------------------------------------------------------------

def test_legacy_legacy_match_passes_flag_default() -> None:
    """The migration-window population: exactly the pre-dispatch behavior."""
    payload = _legacy_stamped_payload()
    scorer_meta = {"model_content_fingerprint": payload["model_content_fingerprint"]}
    cal = _calibrator(
        {"scorer_model_content_fingerprint": payload["model_content_fingerprint"]}
    )
    _assert_calibrator_matches_scorer(
        _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
    )


def test_legacy_legacy_prefix_acceptance_preserved() -> None:
    """The historical 12-char-prefix acceptance survives ON THE LEGACY
    ROUTE ONLY (it dies with the flag at step 4, design §5 row 6)."""
    full = _legacy_hash(_payload())
    scorer_meta = {"model_content_fingerprint": full}
    cal = _calibrator(
        {"scorer_model_content_fingerprint": fd.normalize_fingerprint(full)[:12]}
    )
    _assert_calibrator_matches_scorer(
        _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
    )


def test_legacy_legacy_mismatch_fails_closed() -> None:
    scorer_meta = {"model_content_fingerprint": "sha256:" + "a" * 64}
    cal = _calibrator({"scorer_model_content_fingerprint": "sha256:" + "b" * 64})
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_v1_v1_match_passes() -> None:
    payload = _v1_stamped_payload()
    scorer_meta = {
        "model_content_fingerprint": payload["model_content_fingerprint"],
        "fingerprint_schema_version": payload["fingerprint_schema_version"],
    }
    cal = _calibrator({
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
        "scorer_fingerprint_schema_version": 1,
    })
    _assert_calibrator_matches_scorer(
        _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
    )


def test_v1_v1_mismatch_fails_closed_with_route() -> None:
    scorer_meta = {
        "model_content_fingerprint": "sha256:" + "a" * 64,
        "fingerprint_schema_version": 1,
    }
    cal = _calibrator({
        "scorer_model_content_fingerprint": "sha256:" + "b" * 64,
        "scorer_fingerprint_schema_version": 1,
    })
    with pytest.raises(ValueError, match="route=v1"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_v1_route_has_no_prefix_acceptance() -> None:
    """Exact digest match only on the v1 route (design §5 row 6)."""
    full = _v1_hash(_payload())
    scorer_meta = {
        "model_content_fingerprint": full,
        "fingerprint_schema_version": 1,
    }
    cal = _calibrator({
        "scorer_model_content_fingerprint": fd.normalize_fingerprint(full)[:12],
        "scorer_fingerprint_schema_version": 1,
    })
    with pytest.raises(ValueError, match="route=v1"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_cross_schema_never_matches_even_when_legacy_hash_agrees() -> None:
    """THE no-OR proof: a v1-stamped scorer is never accepted via a
    legacy declaration, even one that equals its true legacy hash."""
    payload = _payload()
    scorer_meta = {
        "model_content_fingerprint": _v1_hash(payload),
        "fingerprint_schema_version": 1,
    }
    cal = _calibrator({
        # The calibrator's versionless declaration IS the scorer's real
        # legacy hash — under an OR-accepting window this would pass.
        "scorer_model_content_fingerprint": _legacy_hash(payload),
    })
    with pytest.raises(ValueError, match="route=cross-schema"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_cross_schema_other_direction_never_matches() -> None:
    payload = _payload()
    scorer_meta = {"model_content_fingerprint": _legacy_hash(payload)}
    cal = _calibrator({
        "scorer_model_content_fingerprint": _v1_hash(payload),
        "scorer_fingerprint_schema_version": 1,
    })
    with pytest.raises(ValueError, match="route=cross-schema"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_flag_off_versionless_fails_with_restamp_remedy() -> None:
    """Step-4 strictness: only the v1 route exists; the error carries the
    VersionGapError-style remedy, not a content-mismatch framing."""
    payload = _legacy_stamped_payload()
    scorer_meta = {"model_content_fingerprint": payload["model_content_fingerprint"]}
    cal = _calibrator(
        {"scorer_model_content_fingerprint": payload["model_content_fingerprint"]}
    )
    with pytest.raises(ValueError, match="re-stamp"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta, accept_legacy=False), cal, Path("cal.json"),
            strict=True,
        )


def test_flag_off_v1_v1_still_passes() -> None:
    payload = _v1_stamped_payload()
    scorer_meta = {
        "model_content_fingerprint": payload["model_content_fingerprint"],
        "fingerprint_schema_version": 1,
    }
    cal = _calibrator({
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
        "scorer_fingerprint_schema_version": 1,
    })
    _assert_calibrator_matches_scorer(
        _ctx(scorer_meta, accept_legacy=False), cal, Path("cal.json"),
        strict=True,
    )


def test_malformed_schema_version_fails_closed() -> None:
    scorer_meta = {
        "model_content_fingerprint": "sha256:" + "a" * 64,
        "fingerprint_schema_version": 2,
    }
    cal = _calibrator({"scorer_model_content_fingerprint": "sha256:" + "a" * 64})
    with pytest.raises(ValueError, match="schema version gap"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_missing_fingerprints_message_unchanged() -> None:
    scorer_meta = {"trained_date": "2026-06-01"}  # no identity keys
    cal = _calibrator({"scorer_model_content_fingerprint": "sha256:" + "b" * 64})
    with pytest.raises(ValueError, match="missing scorer/calibrator"):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )


def test_non_strict_and_no_scorer_meta_still_skip() -> None:
    """Pre-existing escape hatches unchanged by the dispatch."""
    cal = _calibrator({})
    _assert_calibrator_matches_scorer(
        _ctx({"model_content_fingerprint": "x"}), cal, Path("cal.json"),
        strict=False,
    )
    _assert_calibrator_matches_scorer(
        _ctx(None), cal, Path("cal.json"), strict=True,
    )


def test_verify_telemetry_line_emitted(caplog) -> None:
    payload = _legacy_stamped_payload()
    scorer_meta = {"model_content_fingerprint": payload["model_content_fingerprint"]}
    cal = _calibrator(
        {"scorer_model_content_fingerprint": payload["model_content_fingerprint"]}
    )
    with caplog.at_level(logging.INFO):
        _assert_calibrator_matches_scorer(
            _ctx(scorer_meta), cal, Path("cal.json"), strict=True,
        )
    lines = [r.message for r in caplog.records
             if "fingerprint-dispatch verify" in r.message]
    assert lines, "step-1 divergence telemetry line missing"
    assert "route=legacy" in lines[0]


# ---------------------------------------------------------------------------
# WF per-fold binding check (_assert_calibrator_matches_entry, site 3+4)
# ---------------------------------------------------------------------------

def _wf_tree(
    tmp_path: Path,
    scorer_payload: dict,
    cal_meta: dict,
) -> Path:
    """Write scorer + calibrator + manifest; return the manifest path."""
    _write(tmp_path, scorer_payload, "scorer.json")
    cal = GlobalPanelCalibration(
        [0.0, 1.0], [0.4, 0.7], [0.0, 1.0], [0.01, 0.03], metadata=cal_meta,
    )
    cal.save(tmp_path / "calibrator.json")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "retrains": [{
            "cutoff_date": "2024-01-02",
            "trained_date": "2024-01-03",
            "artifact_uri": "scorer.json",
            "calibrator_uri": "calibrator.json",
        }]
    }), encoding="utf-8")
    return manifest


def test_wf_legacy_stamped_fold_matches_legacy_calibrator(tmp_path) -> None:
    payload = _legacy_stamped_payload()
    manifest = _wf_tree(tmp_path, payload, {
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
    })
    cal = WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")
    assert isinstance(cal, GlobalPanelCalibration)


def test_wf_v1_stamped_fold_matches_v1_calibrator(tmp_path) -> None:
    payload = _v1_stamped_payload()
    manifest = _wf_tree(tmp_path, payload, {
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
        "scorer_fingerprint_schema_version": 1,
    })
    cal = WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")
    assert isinstance(cal, GlobalPanelCalibration)


def test_wf_v1_stamped_fold_with_corrupt_stamp_fails_at_read(tmp_path) -> None:
    """A v1-stamped fold is verify()'d against its own payload — a corrupt
    stamp is a MismatchError (ValueError), fail-closed, unlike the old
    fail-soft recompute swallow at the pre-dispatch loader.py:158."""
    payload = _v1_stamped_payload()
    payload["model_content_fingerprint"] = "sha256:" + "0" * 64
    manifest = _wf_tree(tmp_path, payload, {
        "scorer_model_content_fingerprint": "sha256:" + "0" * 64,
        "scorer_fingerprint_schema_version": 1,
    })
    with pytest.raises(ValueError, match="mismatch"):
        WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")


def test_wf_cross_schema_fails(tmp_path) -> None:
    payload = _v1_stamped_payload()
    legacy_of_same = _legacy_hash(_payload())
    manifest = _wf_tree(tmp_path, payload, {
        # Versionless declaration equal to the fold's true legacy hash:
        # never accepted against a v1-stamped fold.
        "scorer_model_content_fingerprint": legacy_of_same,
    })
    with pytest.raises(ValueError, match="route=cross-schema"):
        WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")


def test_wf_unstamped_fold_current_behavior_both_recomputes(tmp_path) -> None:
    """Unstamped fallback: the pre-existing recompute acceptance, made
    explicit + venv-independent (legacy shim AND v1 recomputes both
    acceptable). Production is never unstamped post step-0; this is the
    dev/test-fixture state."""
    unstamped = _payload()
    for declared in (_legacy_hash(unstamped), _v1_hash(unstamped)):
        d = tmp_path / declared[-8:]
        d.mkdir()
        manifest = _wf_tree(d, dict(unstamped), {
            "scorer_model_content_fingerprint": declared,
        })
        cal = WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")
        assert isinstance(cal, GlobalPanelCalibration)


def test_wf_flag_off_versionless_fails_with_remedy(tmp_path) -> None:
    payload = _legacy_stamped_payload()
    manifest = _wf_tree(tmp_path, payload, {
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
    })
    loader = WalkForwardModelLoader(manifest, accept_legacy_stamps=False)
    with pytest.raises(ValueError, match="re-stamp"):
        loader.calibrator_as_of("2024-01-10")


def test_wf_flag_off_v1_pair_passes(tmp_path) -> None:
    payload = _v1_stamped_payload()
    manifest = _wf_tree(tmp_path, payload, {
        "scorer_model_content_fingerprint": payload["model_content_fingerprint"],
        "scorer_fingerprint_schema_version": 1,
    })
    loader = WalkForwardModelLoader(manifest, accept_legacy_stamps=False)
    assert isinstance(
        loader.calibrator_as_of("2024-01-10"), GlobalPanelCalibration,
    )


def test_wf_missing_declaration_message_unchanged(tmp_path) -> None:
    payload = _legacy_stamped_payload()
    manifest = _wf_tree(tmp_path, payload, {"note": "no declaration"})
    with pytest.raises(ValueError, match="missing scorer/calibrator fingerprint"):
        WalkForwardModelLoader(manifest).calibrator_as_of("2024-01-10")


# ---------------------------------------------------------------------------
# PanelScorer.load — both identities stamped + fail-closed v1 verify
# ---------------------------------------------------------------------------

def _real_booster_payload(base: dict) -> dict:
    xgb = pytest.importorskip("xgboost")
    dtrain = xgb.DMatrix([[1.0, 0.2], [0.8, 0.1]], label=[1.0, 0.0])
    booster = xgb.train(
        {"objective": "reg:squarederror", "max_depth": 1, "nthread": 1,
         "verbosity": 0},
        dtrain, num_boost_round=1, verbose_eval=False,
    )
    payload = dict(base)
    payload["feature_cols"] = ["alpha_1", "alpha_2"]
    payload["booster_raw_json"] = bytes(
        booster.save_raw(raw_format="json")
    ).decode("utf-8")
    return payload


def test_panel_scorer_load_stamps_both_identities(tmp_path) -> None:
    from renquant_pipeline.kernel.panel_pipeline.panel_scorer import PanelScorer

    payload = _real_booster_payload(_payload())
    payload["model_content_fingerprint"] = _legacy_hash(payload)
    p = _write(tmp_path, payload, "artifact.json")

    scorer = PanelScorer.load(p)
    meta = scorer.metadata
    # Legacy identity: the stamped value, preserved (window behavior).
    assert meta["model_content_fingerprint"] == payload["model_content_fingerprint"]
    # Both recomputes stamped under telemetry-only keys (never collected
    # by _fingerprint_values → can never leak into a legacy-route match).
    assert meta[fd.META_LEGACY_RECOMPUTE] == payload["model_content_fingerprint"]
    assert meta[fd.META_V1_RECOMPUTE] == _v1_hash(payload)
    assert meta[fd.META_V1_RECOMPUTE] != meta[fd.META_LEGACY_RECOMPUTE]


def test_panel_scorer_load_verifies_v1_stamp_fail_closed(tmp_path) -> None:
    from renquant_pipeline.kernel.panel_pipeline.panel_scorer import PanelScorer

    payload = _real_booster_payload(_payload())
    payload.update(shared.stamp(payload))
    good = _write(tmp_path, payload, "good.json")
    scorer = PanelScorer.load(good)
    assert scorer.metadata["fingerprint_schema_version"] == 1

    corrupt = dict(payload)
    corrupt["model_content_fingerprint"] = "sha256:" + "0" * 64
    bad = _write(tmp_path, corrupt, "bad.json")
    with pytest.raises(ValueError, match="mismatch"):
        PanelScorer.load(bad)


def test_panel_scorer_load_unclassified_key_is_telemetry_not_fatal(tmp_path) -> None:
    """An UNSTAMPED artifact with an unclassified key keeps loading (the
    v1 recompute is telemetry during the window); the error is recorded."""
    from renquant_pipeline.kernel.panel_pipeline.panel_scorer import PanelScorer

    payload = _real_booster_payload(_payload())
    payload["a_key_no_table_classifies"] = 1
    p = _write(tmp_path, payload, "artifact.json")
    scorer = PanelScorer.load(p)
    assert "UnclassifiedKeyError" in scorer.metadata[fd.META_V1_RECOMPUTE_ERROR]
    assert fd.META_V1_RECOMPUTE not in scorer.metadata
