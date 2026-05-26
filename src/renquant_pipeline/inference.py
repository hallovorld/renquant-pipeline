"""Runtime inference-pipeline contract.

The current 104 implementation is ported behind these stages in reviewed
slices. This module pins the top-level contract first so execution and
backtesting can share the same runtime flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
