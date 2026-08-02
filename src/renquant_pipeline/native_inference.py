"""Native inference snapshot facade for already-hydrated live contexts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .inference import LiveContextSnapshot, live_context_snapshot_from_live_context
from .serving_features import write_staged_serving_features


class _RunnablePipeline(Protocol):
    def run(self, ctx: Any) -> Any: ...


def _default_pipeline(*, sell_only: bool) -> _RunnablePipeline:
    from .kernel.pipeline import InferencePipeline, SellOnlyPipeline

    return SellOnlyPipeline() if sell_only else InferencePipeline()


def run_native_inference_snapshot(
    ctx: Any,
    *,
    sell_only: bool = False,
    output_json: str | Path | None = None,
    pipeline: _RunnablePipeline | None = None,
) -> LiveContextSnapshot:
    """Run native pipeline code on a supplied context and return a snapshot.

    The caller owns context hydration: market data, holdings, prices, models,
    and config must already be present. This function does not import umbrella
    live runner code, submit orders, or mutate persistent live state.
    """
    runner = pipeline or _default_pipeline(sell_only=sell_only)
    runner.run(ctx)
    out: Path | None = None
    if output_json is not None:
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        # pipeline#250 rollout step 2 (codex on #252): this facade is the
        # surface renquant-orchestrator's native_live_inference consumes, so
        # it must finalize a staged serving-features write exactly like the
        # inference.py payload writers do — the payload's parent dir IS the
        # run output dir. Completed BEFORE the snapshot is built so both the
        # snapshot and the written payload carry the sidecar block. No-op
        # (None) when nothing was staged; never raises (record-don't-raise).
        write_staged_serving_features(ctx, out.parent)
    snapshot = live_context_snapshot_from_live_context(ctx)
    if out is not None:
        out.write_text(
            json.dumps(snapshot.to_runtime_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return snapshot


__all__ = ["run_native_inference_snapshot"]
