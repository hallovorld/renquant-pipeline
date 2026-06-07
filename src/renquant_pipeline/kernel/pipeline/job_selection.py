"""SelectionJob — greedy slot-filling with tiered thresholds and all guards."""
from __future__ import annotations

from .context import InferenceContext
from .pipeline import Job, Task
from .task_selection import (
    ApplyBearDefensiveSleeveTask,
    PrepareSelectionTask,
    RunSelectionTask,
    SizeAndEmitTask,
)


class SelectionJob(Job):
    """Task chain: PrepareSelection → RunSelection → SizeAndEmit → BEAR sleeve."""

    def should_skip(self, ctx: InferenceContext) -> bool:
        if ctx.ranked:
            return False
        return not ApplyBearDefensiveSleeveTask.is_enabled(ctx)

    @property
    def tasks(self) -> list[Task]:
        return [
            PrepareSelectionTask(),
            RunSelectionTask(),
            SizeAndEmitTask(),
            ApplyBearDefensiveSleeveTask(),
        ]
