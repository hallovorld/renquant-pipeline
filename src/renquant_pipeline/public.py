"""Narrow public surface for cross-repo consumers (V-005 remediation).

Only types that a sibling repo demonstrably needs belong here.  Each
import is LAZY (loaded on first attribute access) so that importing this
module does not eagerly pull in unrelated kernel subsystems.

Current consumers (orchestrator ``native_context_hydration.py``):
  - ``LocalStore``   — kernel.data
  - ``HoldingState``  — kernel.exits
  - ``RegimeState``   — kernel.regime

Symbols NOT exported here (codex review on this PR):
  - ``LoadUniverseJob`` / ``UniverseContext`` — pipeline execution
    internals; orchestrator should not construct pipeline job objects.
  - ``record_training_run`` — training/model-run persistence; its
    consumer (``train_gbdt.py``) is itself model-training logic that
    belongs in renquant-model, not orchestrator.
  - ``_last_completed_nyse_session`` — use
    ``renquant_common.market_calendar`` instead.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from renquant_pipeline.kernel.data import LocalStore as _LocalStore
    from renquant_pipeline.kernel.exits import HoldingState as _HoldingState
    from renquant_pipeline.kernel.regime import RegimeState as _RegimeState

__all__ = [
    "LocalStore",
    "HoldingState",
    "RegimeState",
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
