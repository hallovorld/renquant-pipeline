"""Native inference snapshot facade for already-hydrated live contexts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .inference import LiveContextSnapshot, live_context_snapshot_from_live_context


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
    snapshot = live_context_snapshot_from_live_context(ctx)
    if output_json is not None:
        out = Path(output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(snapshot.to_runtime_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return snapshot


__all__ = ["run_native_inference_snapshot"]
