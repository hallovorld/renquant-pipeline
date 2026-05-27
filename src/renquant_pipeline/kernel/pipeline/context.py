"""Re-export shim for the pipeline decision-tree context.

The canonical ``InferenceContext`` / ``TickerInferenceContext`` dataclasses
live in :mod:`renquant_pipeline.context` (lifted in P1 with ``RegimeLabel``
adopted). The umbrella's ``kernel/pipeline/*`` Tasks/Jobs import them via
``from .context import ...``; this shim makes that relative import resolve
inside the lifted ``renquant_pipeline.kernel.pipeline`` package without
copying the dataclass definition (single source of truth — not a §5.13.5
duplicate).
"""
from __future__ import annotations

from renquant_pipeline.context import InferenceContext, TickerInferenceContext

__all__ = ["InferenceContext", "TickerInferenceContext"]
