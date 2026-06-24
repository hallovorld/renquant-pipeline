"""Regression tests for `_sentiment_runtime_gate_declared`.

2026-06-23 train/serve bug: the panel_ltr_xgboost scorer exposes ``.artifact``
but NOT ``.metadata`` (its dataclass fields are artifact/booster/feature_cols),
and the runtime-zeroing contract is stamped at the artifact TOP LEVEL. The old
check read only ``scorer.metadata`` → always {} → declared-absent → sentiment
features were LEFT UNCHANGED in disabled regimes (e.g. BULL_CALM) even though
the model was trained with them zeroed there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    _sentiment_runtime_gate_declared,
)


@dataclass
class _ArtifactScorer:
    """Mirrors PanelLtrXgboostScorer: exposes .artifact, has NO .metadata."""
    artifact: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MetadataScorer:
    """A scorer type that carries the contract under .metadata instead."""
    metadata: dict[str, Any] = field(default_factory=dict)


def test_contract_at_artifact_top_level_is_recognized():
    # the live panel-ltr artifact shape — the regression case
    scorer = _ArtifactScorer(artifact={
        "sentiment_runtime_gate_contract": "trained_zeroing",
        "sentiment_runtime_gate_disabled_regimes": ["BULL_CALM"],
    })
    assert _sentiment_runtime_gate_declared(scorer) is True


def test_runtime_zeroing_value_also_recognized():
    scorer = _ArtifactScorer(artifact={"sentiment_runtime_gate_contract": "runtime_zeroing"})
    assert _sentiment_runtime_gate_declared(scorer) is True


def test_trained_flag_at_artifact_level():
    scorer = _ArtifactScorer(artifact={"sentiment_runtime_gate_trained": True})
    assert _sentiment_runtime_gate_declared(scorer) is True


def test_metadata_fallback_still_works():
    # scorer types that expose .metadata must still be honored
    scorer = _MetadataScorer(metadata={"sentiment_runtime_gate_contract": "trained_zeroing"})
    assert _sentiment_runtime_gate_declared(scorer) is True


def test_absent_contract_returns_false():
    assert _sentiment_runtime_gate_declared(_ArtifactScorer(artifact={})) is False
    assert _sentiment_runtime_gate_declared(_MetadataScorer(metadata={})) is False


def test_unrelated_contract_string_returns_false():
    scorer = _ArtifactScorer(artifact={"sentiment_runtime_gate_contract": "none"})
    assert _sentiment_runtime_gate_declared(scorer) is False


def test_scorer_without_artifact_or_metadata_does_not_crash():
    class _Bare:
        pass
    assert _sentiment_runtime_gate_declared(_Bare()) is False
