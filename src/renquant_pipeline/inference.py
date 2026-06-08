"""Runtime inference-pipeline contract.

The current 104 implementation is ported behind these stages in reviewed
slices. This module pins the top-level contract first so execution and
backtesting can share the same runtime flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from renquant_common import Job, Pipeline, Task
from renquant_artifacts import validate_artifact_manifest
from renquant_base_data import validate_data_manifest


@dataclass
class InferenceContext:
    strategy_config: dict[str, Any]
    data_manifest: dict[str, Any]
    artifact_manifest: dict[str, Any]
    market_snapshot: dict[str, Any]
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    decision_trace: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    order_intents: list[dict[str, Any]] = field(default_factory=list)
    blocked_by: dict[str, str] = field(default_factory=dict)
    buy_blocked: bool = False


class ValidateRuntimeInputsTask(Task):
    """Require auditable config/artifact/market inputs before scoring."""

    def run(self, ctx: InferenceContext) -> bool | None:
        if not ctx.strategy_config.get("watchlist"):
            raise ValueError("strategy_config missing watchlist")
        validate_data_manifest(ctx.data_manifest)
        validate_artifact_manifest(ctx.artifact_manifest)
        if not ctx.market_snapshot.get("as_of"):
            raise ValueError("market_snapshot missing as_of")
        return True


class RuntimeStageTask(Task):
    """Adapter task for dependency-injected runtime stages."""

    def __init__(self, name: str, fn) -> None:
        self._name = name
        self.fn = fn

    @property
    def name(self) -> str:
        return self._name

    def run(self, ctx: InferenceContext) -> bool | None:
        return self.fn(ctx)


class RuntimeInferenceJob(Job):
    def __init__(self, stages: list[Task]) -> None:
        self._tasks = [ValidateRuntimeInputsTask(), *stages]

    @property
    def tasks(self) -> list[Task]:
        return self._tasks


class RuntimeInferencePipeline(Pipeline):
    """Top-level runtime pipeline shared by live, shadow, sim, and LEAN."""

    def __init__(self, stages: list[Task]) -> None:
        super().__init__([RuntimeInferenceJob(stages)], name="runtime-inference")


def runtime_inference_payload(ctx: InferenceContext) -> dict[str, Any]:
    """Return the JSON payload consumed by native live-bundle tooling."""
    return {
        "schema_version": 1,
        "source": "renquant_pipeline.runtime_inference",
        "market_as_of": ctx.market_snapshot.get("as_of"),
        "decision_trace": list(ctx.decision_trace),
        "order_intents": list(ctx.order_intents),
        "scores": dict(ctx.scores),
        "blocked_by": dict(ctx.blocked_by),
        "buy_blocked": bool(ctx.buy_blocked),
    }


def write_runtime_inference_payload(ctx: InferenceContext, path: str | Path) -> Path:
    """Write the runtime inference payload as deterministic JSON."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(runtime_inference_payload(ctx), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out
