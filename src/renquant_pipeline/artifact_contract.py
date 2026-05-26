"""Compatibility exports for artifact contracts.

The canonical implementation lives in ``renquant_artifacts.contracts`` so
training, runtime, and backtesting share one source of truth for model
artifact evidence and run provenance.
"""
from __future__ import annotations

from renquant_artifacts.contracts import (
    PANEL_REQUIRED_FIELDS,
    PANEL_STRICT_FIELDS,
    SENTIMENT_DEFAULT_REGIME_POLICY,
    SENTIMENT_FEATURE_COLS,
    SENTIMENT_RUNTIME_GATE_CONTRACTS,
    ContractResult,
    build_run_bundle,
    hash_jsonable,
    has_sentiment_runtime_gate_contract,
    resolve_artifact_paths,
    sentiment_effective_regime_policy,
    sentiment_runtime_gate_requirement,
    sha256_file,
    validate_feature_contract,
    validate_panel_artifact_contract,
)

__all__ = [
    "PANEL_REQUIRED_FIELDS",
    "PANEL_STRICT_FIELDS",
    "SENTIMENT_DEFAULT_REGIME_POLICY",
    "SENTIMENT_FEATURE_COLS",
    "SENTIMENT_RUNTIME_GATE_CONTRACTS",
    "ContractResult",
    "build_run_bundle",
    "hash_jsonable",
    "has_sentiment_runtime_gate_contract",
    "resolve_artifact_paths",
    "sentiment_effective_regime_policy",
    "sentiment_runtime_gate_requirement",
    "sha256_file",
    "validate_feature_contract",
    "validate_panel_artifact_contract",
]
