"""RenQuant runtime decision pipeline package.

Lazy (PEP 562) — same pattern and rationale as
``kernel/panel_pipeline/__init__.py``: importing ANY submodule of this
package (e.g. ``renquant_pipeline.bundle_contract``, the GOAL-5 AC4
public pair-validation API that the renquant-artifacts bundle store
imports) forces Python to run this ``__init__`` first, regardless of what
that submodule itself needs. The previous eager imports made
``import renquant_pipeline.bundle_contract`` a hard pandas/numpy/scipy/
renquant_artifacts dependency (~1.7s) for a validator that needs only
stdlib + ``renquant_common.model_fingerprint`` (RFC RenQuant#492 §2.5:
the public API must be import-light). Attribute access below works
exactly as documented; only the *type* of import (name access vs. module
load) changed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

# §3.5 canonical-path policy note (unchanged by the lazy refactor): the
# artifact contracts re-exported here (``ContractResult`` etc.) live in
# renquant-artifacts; the lazy map resolves straight to the canonical
# source — no internal shim.
_LAZY = {
    # .inference
    "InferenceContext": (".inference", "InferenceContext"),
    "LiveContextSnapshot": (".inference", "LiveContextSnapshot"),
    "RuntimeInferencePipeline": (".inference", "RuntimeInferencePipeline"),
    "live_context_snapshot_from_live_context": (
        ".inference", "live_context_snapshot_from_live_context"),
    "runtime_inference_payload": (".inference", "runtime_inference_payload"),
    "runtime_inference_payload_from_live_context": (
        ".inference", "runtime_inference_payload_from_live_context"),
    "write_runtime_inference_payload": (
        ".inference", "write_runtime_inference_payload"),
    "write_runtime_inference_payload_from_live_context": (
        ".inference", "write_runtime_inference_payload_from_live_context"),
    # .live_state_contract
    "LiveStateContract": (".live_state_contract", "LiveStateContract"),
    "account_snapshot_from_live_state": (
        ".live_state_contract", "account_snapshot_from_live_state"),
    "load_live_state_contract": (
        ".live_state_contract", "load_live_state_contract"),
    # .model_admission
    "ModelAdmissionResult": (".model_admission", "ModelAdmissionResult"),
    "evaluate_model_admission": (".model_admission", "evaluate_model_admission"),
    # .native_inference
    "run_native_inference_snapshot": (
        ".native_inference", "run_native_inference_snapshot"),
    # .decision_trace
    "active_scorer_identity": (".decision_trace", "active_scorer_identity"),
    "append_ticker_daily_state_rows": (
        ".decision_trace", "append_ticker_daily_state_rows"),
    "build_ticker_daily_state_rows": (
        ".decision_trace", "build_ticker_daily_state_rows"),
    "model_type_from_artifact": (".decision_trace", "model_type_from_artifact"),
    # .order_attribution
    "ATTRIBUTION_VERSION": (".order_attribution", "ATTRIBUTION_VERSION"),
    "score_snapshot": (".order_attribution", "score_snapshot"),
    "stamp_order_attribution": (".order_attribution", "stamp_order_attribution"),
    "validate_order_attribution": (
        ".order_attribution", "validate_order_attribution"),
    # .panel_scoring
    "ApplyGlobalCalibrationTask": (".panel_scoring", "ApplyGlobalCalibrationTask"),
    "ApplyScoresTask": (".panel_scoring", "ApplyScoresTask"),
    "BuildFeatureMatrixTask": (".panel_scoring", "BuildFeatureMatrixTask"),
    "EmitAttributedOrderIntentsTask": (
        ".panel_scoring", "EmitAttributedOrderIntentsTask"),
    "LoadScorerTask": (".panel_scoring", "LoadScorerTask"),
    "PanelScoringJob": (".panel_scoring", "PanelScoringJob"),
    "RegimeModelAdmissionTask": (".panel_scoring", "RegimeModelAdmissionTask"),
    "VetoWeakBuysTask": (".panel_scoring", "VetoWeakBuysTask"),
    # .runtime_features
    "build_runtime_feature_frame": (
        ".runtime_features", "build_runtime_feature_frame"),
    "transform_feature_rows": (".runtime_features", "transform_feature_rows"),
    # .selection
    "SelectAcceptedCandidatesTask": (
        ".selection", "SelectAcceptedCandidatesTask"),
    "SelectionJob": (".selection", "SelectionJob"),
    "ValidateSelectionDoesNotPromoteTask": (
        ".selection", "ValidateSelectionDoesNotPromoteTask"),
    # renquant_artifacts.contracts (canonical source, §3.5)
    "ContractResult": ("renquant_artifacts.contracts", "ContractResult"),
    "build_run_bundle": ("renquant_artifacts.contracts", "build_run_bundle"),
    "hash_jsonable": ("renquant_artifacts.contracts", "hash_jsonable"),
    "validate_feature_contract": (
        "renquant_artifacts.contracts", "validate_feature_contract"),
    "validate_model_evidence_contract": (
        "renquant_artifacts.contracts", "validate_model_evidence_contract"),
    "validate_panel_artifact_contract": (
        "renquant_artifacts.contracts", "validate_panel_artifact_contract"),
    # .state_paths
    "live_state_legacy_path": (".state_paths", "live_state_legacy_path"),
    "live_state_path": (".state_paths", "live_state_path"),
    "resolve_live_state_read": (".state_paths", "resolve_live_state_read"),
    "runs_db_legacy_path": (".state_paths", "runs_db_legacy_path"),
    "runs_db_path": (".state_paths", "runs_db_path"),
    # .software_stops
    "DEFAULT_MAX_STALENESS_MINUTES": (
        ".software_stops", "DEFAULT_MAX_STALENESS_MINUTES"),
    "SoftwareStopRegistry": (".software_stops", "SoftwareStopRegistry"),
    "SoftwareStopRegistryCorrupt": (
        ".software_stops", "SoftwareStopRegistryCorrupt"),
    "compute_staleness": (".software_stops", "compute_staleness"),
    "registry_path_for": (".software_stops", "registry_path_for"),
}

__all__ = sorted(_LAZY)

if TYPE_CHECKING:  # static analysers see the eager surface unchanged
    from renquant_artifacts.contracts import (
        ContractResult,
        build_run_bundle,
        hash_jsonable,
        validate_feature_contract,
        validate_model_evidence_contract,
        validate_panel_artifact_contract,
    )

    from .decision_trace import (
        active_scorer_identity,
        append_ticker_daily_state_rows,
        build_ticker_daily_state_rows,
        model_type_from_artifact,
    )
    from .inference import (
        InferenceContext,
        LiveContextSnapshot,
        RuntimeInferencePipeline,
        live_context_snapshot_from_live_context,
        runtime_inference_payload,
        runtime_inference_payload_from_live_context,
        write_runtime_inference_payload,
        write_runtime_inference_payload_from_live_context,
    )
    from .live_state_contract import (
        LiveStateContract,
        account_snapshot_from_live_state,
        load_live_state_contract,
    )
    from .model_admission import ModelAdmissionResult, evaluate_model_admission
    from .native_inference import run_native_inference_snapshot
    from .order_attribution import (
        ATTRIBUTION_VERSION,
        score_snapshot,
        stamp_order_attribution,
        validate_order_attribution,
    )
    from .panel_scoring import (
        ApplyGlobalCalibrationTask,
        ApplyScoresTask,
        BuildFeatureMatrixTask,
        EmitAttributedOrderIntentsTask,
        LoadScorerTask,
        PanelScoringJob,
        RegimeModelAdmissionTask,
        VetoWeakBuysTask,
    )
    from .runtime_features import build_runtime_feature_frame, transform_feature_rows
    from .selection import (
        SelectAcceptedCandidatesTask,
        SelectionJob,
        ValidateSelectionDoesNotPromoteTask,
    )
    from .software_stops import (
        DEFAULT_MAX_STALENESS_MINUTES,
        SoftwareStopRegistry,
        SoftwareStopRegistryCorrupt,
        compute_staleness,
        registry_path_for,
    )
    from .state_paths import (
        live_state_legacy_path,
        live_state_path,
        resolve_live_state_read,
        runs_db_legacy_path,
        runs_db_path,
    )


def __getattr__(name: str):
    import importlib

    try:
        module_name, attr_name = _LAZY[name]
    except KeyError:
        # Submodule fallback: under the eager __init__, importing the
        # package bound its imported submodules as attributes as a side
        # effect (e.g. ``renquant_pipeline.inference``). Keep that
        # attribute-style access working.
        try:
            return importlib.import_module(f".{name}", __name__)
        except ModuleNotFoundError:
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r}"
            ) from None
    if module_name.startswith("."):
        module = importlib.import_module(module_name, __name__)
    else:
        module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value  # cache on the package module, PEP 562 style
    return value


def __dir__():
    return sorted(set(list(globals()) + list(_LAZY)))
