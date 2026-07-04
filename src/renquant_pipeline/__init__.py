"""RenQuant runtime decision pipeline package."""

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
from .decision_trace import (
    active_scorer_identity,
    append_ticker_daily_state_rows,
    build_ticker_daily_state_rows,
    model_type_from_artifact,
)
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
# §3.5 canonical-path policy: artifact contracts live in renquant-artifacts.
# Subrepo re-exports for the public ``renquant_pipeline.*`` API, but the
# import resolves straight to the canonical source — no internal shim.
from renquant_artifacts.contracts import (
    ContractResult,
    build_run_bundle,
    hash_jsonable,
    validate_feature_contract,
    validate_model_evidence_contract,
    validate_panel_artifact_contract,
)
from .state_paths import (
    live_state_legacy_path,
    live_state_path,
    resolve_live_state_read,
    runs_db_legacy_path,
    runs_db_path,
)
from .software_stops import (
    DEFAULT_MAX_STALENESS_MINUTES,
    SoftwareStopRegistry,
    SoftwareStopRegistryCorrupt,
    compute_staleness,
    registry_path_for,
)

__all__ = [
    "ContractResult",
    "DEFAULT_MAX_STALENESS_MINUTES",
    "InferenceContext",
    "LiveStateContract",
    "LiveContextSnapshot",
    "ModelAdmissionResult",
    "SoftwareStopRegistry",
    "SoftwareStopRegistryCorrupt",
    "ATTRIBUTION_VERSION",
    "ApplyGlobalCalibrationTask",
    "ApplyScoresTask",
    "BuildFeatureMatrixTask",
    "EmitAttributedOrderIntentsTask",
    "LoadScorerTask",
    "PanelScoringJob",
    "RegimeModelAdmissionTask",
    "RuntimeInferencePipeline",
    "SelectAcceptedCandidatesTask",
    "SelectionJob",
    "ValidateSelectionDoesNotPromoteTask",
    "VetoWeakBuysTask",
    "active_scorer_identity",
    "account_snapshot_from_live_state",
    "append_ticker_daily_state_rows",
    "build_run_bundle",
    "build_runtime_feature_frame",
    "build_ticker_daily_state_rows",
    "compute_staleness",
    "evaluate_model_admission",
    "hash_jsonable",
    "live_state_legacy_path",
    "live_state_path",
    "load_live_state_contract",
    "live_context_snapshot_from_live_context",
    "model_type_from_artifact",
    "registry_path_for",
    "resolve_live_state_read",
    "runtime_inference_payload",
    "runtime_inference_payload_from_live_context",
    "run_native_inference_snapshot",
    "runs_db_legacy_path",
    "runs_db_path",
    "score_snapshot",
    "stamp_order_attribution",
    "transform_feature_rows",
    "validate_feature_contract",
    "validate_model_evidence_contract",
    "validate_order_attribution",
    "validate_panel_artifact_contract",
    "write_runtime_inference_payload",
    "write_runtime_inference_payload_from_live_context",
]
