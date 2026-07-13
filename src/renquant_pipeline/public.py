"""Narrow public surface for cross-repo consumers (V-005 remediation).

Only types that a sibling repo demonstrably needs belong here.  Each
import is LAZY (loaded on first attribute access, or function-scoped for
operations) so that importing this module does not eagerly pull in
unrelated kernel subsystems.

Current consumers (orchestrator ``native_context_hydration.py``):
  - ``LocalStore``     — kernel.data
  - ``HoldingState``    — kernel.exits
  - ``RegimeState``     — kernel.regime
  - ``load_universe``   — narrow OPERATION wrapping
    ``kernel.pipeline.job_universe`` (see below)

``load_universe`` replaces a sibling repo constructing
``LoadUniverseJob``/``UniverseContext`` directly (codex review, pipeline#197
round 1, point 2: "the orchestrator should request a stable pipeline
operation ... not construct pipeline job objects itself"). It runs the
real per-ticker tournament admission chain internally
(``LoadArtifactsTask`` → ``FilterStalenessTask`` → ``FilterUniverseFloorTask``
→ ``FilterAutoDropTask``) and returns only the ``models``/``rejections`` a
caller needs via :class:`UniverseLoadResult` — never the ``LoadUniverseJob``/
``UniverseContext`` objects or their execution lifecycle, so those stay
pipeline-owned internals rather than becoming a permanent cross-repo
contract.

Symbols NOT exported here (codex review on this PR):
  - ``LoadUniverseJob`` / ``UniverseContext`` themselves — pipeline
    execution internals; see ``load_universe`` above for the narrow
    operation a sibling repo should use instead.
  - ``record_training_run`` — training/model-run persistence; its
    consumer (``train_gbdt.py``) is itself model-training logic that
    belongs in renquant-model, not orchestrator.
  - ``_last_completed_nyse_session`` — use
    ``renquant_common.market_calendar`` instead.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from renquant_pipeline.kernel.data import LocalStore as _LocalStore
    from renquant_pipeline.kernel.exits import HoldingState as _HoldingState
    from renquant_pipeline.kernel.regime import RegimeState as _RegimeState

__all__ = [
    "LocalStore",
    "HoldingState",
    "RegimeState",
    "UniverseLoadResult",
    "load_universe",
]

_LAZY_MAP: dict[str, tuple[str, str]] = {
    "LocalStore": ("renquant_pipeline.kernel.data", "LocalStore"),
    "HoldingState": ("renquant_pipeline.kernel.exits", "HoldingState"),
    "RegimeState": ("renquant_pipeline.kernel.regime", "RegimeState"),
}


def __getattr__(name: str):
    entry = _LAZY_MAP.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr = entry
    mod = importlib.import_module(module_path)
    obj = getattr(mod, attr)
    globals()[name] = obj
    return obj


@dataclass(frozen=True)
class UniverseLoadResult:
    """Outcome of the per-ticker tournament universe-admission chain.

    Deliberately narrow: only the two fields a cross-repo caller needs —
    the models admitted for scoring, and the rejected tickers with their
    reasons. Does NOT expose ``UniverseContext``/``LoadUniverseJob``
    themselves (codex review, pipeline#197 round 1, point 2): those stay
    pipeline execution internals, never a permanent cross-repo contract.
    """

    models: dict[str, dict]
    rejections: list[tuple[str, str]]


def load_universe(
    *,
    config: dict[str, Any],
    strategy_dir: "str | Path",
    broker_name: "str | None" = None,
    held_tickers: "set[str] | None" = None,
    as_of_date: Any = None,
) -> UniverseLoadResult:
    """Run the per-ticker tournament admission chain; return the result.

    This is the narrow, pipeline-OWNED operation V-005 requires in place of
    a sibling repo constructing ``LoadUniverseJob``/``UniverseContext``
    directly. It runs the real ``LoadUniverseJob`` chain (artifact load →
    staleness filter → universe-floor filter → auto-drop filter) against a
    fresh ``UniverseContext`` built from the given inputs, and returns only
    the ``models``/``rejections`` a caller needs — not the Job/Context
    objects or their internal execution lifecycle (``fallback_exit`` and
    other ``UniverseContext`` fields stay internal until a real consumer
    need is demonstrated).

    Args:
        config: the resolved strategy config (read-only; not mutated).
        strategy_dir: strategy checkout root; ``strategy_dir/models`` holds
            the per-ticker artifacts.
        broker_name: broker tag for state-file isolation (``None`` for
            sim/lean paths).
        held_tickers: authoritative held tickers from the broker/account
            snapshot. When given (including an empty set), this is
            AUTHORITATIVE and wins over state-file-derived holdings.
            ``None`` falls back to reading ``live_state`` (matches
            ``UniverseContext``'s own default).
        as_of_date: effective session/as-of date for freshness math
            (``None`` → ``date.today()``).

    The import of ``kernel.pipeline.job_universe`` is function-scoped, so
    calling this function is the only thing that pulls in that kernel
    subsystem — importing ``renquant_pipeline.public`` itself does not.
    """
    from renquant_pipeline.kernel.pipeline.job_universe import (  # noqa: PLC0415
        LoadUniverseJob,
        UniverseContext,
    )

    uctx = UniverseContext(
        config=config,
        strategy_dir=Path(strategy_dir),
        broker_name=broker_name,
        held_tickers=(
            set(held_tickers) if held_tickers is not None else None
        ),
        as_of_date=as_of_date,
    )
    LoadUniverseJob().run(uctx)
    return UniverseLoadResult(
        models=dict(uctx.loaded_models),
        rejections=list(uctx.rejections),
    )
