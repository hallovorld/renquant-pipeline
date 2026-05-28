"""Pipeline orchestration core lifted from the umbrella (functional-lift slice 4).

Unlike the pure-leaf slices (verbatim copy-not-move), this slice *reconciles*
the umbrella's duplicate orchestration primitives onto :mod:`renquant_common`:

* ``pipeline`` — re-exports the canonical ``Task`` / ``Job`` / ``run_parallel``
  / ``ParallelTimeoutError`` / ``resolve_workers`` from ``renquant_common`` and
  adds the thin ``TickerJob`` + config-deriving ``run_parallel`` wrapper. This
  removes the duplicate executor loop (RFC Decision item 6: compose common's
  primitives, do not reinvent orchestration).
* ``atoms`` — reusable ``Task`` atoms (ctx ops, gates, logging, numerical
  guards, persistence, vector builders), copied verbatim. They depend only on
  the ``Task`` ABC re-exported by ``pipeline``.

``InferenceContext`` / ``TickerInferenceContext`` already live in
:mod:`renquant_pipeline.context` (lifted in P1 with ``RegimeLabel`` adopted);
this package imports them from there rather than defining a second copy.

Slice 7 — first decision-tree Job + context shim:
* ``context`` — a re-export shim (`from renquant_pipeline.context import ...`)
  so the umbrella Tasks' ``from .context import`` relative import resolves
  verbatim inside this package. Single source of truth, not a duplicate.
* ``job_regime`` / ``task_regime`` / ``task_spy_regime`` /
  ``task_trend_overlay`` — the 3-layer regime detection Job (Hurst → CUSUM →
  GMM → BEAROverride → TrendOverlay → Finalize, + SPY-label). ``job_regime``
  and ``task_trend_overlay`` are verbatim; ``task_regime`` / ``task_spy_regime``
  have only absolute ``kernel.X`` lazy imports rewritten to
  ``renquant_pipeline.kernel.X``. Parity test runs the full Job end-to-end.
"""
from __future__ import annotations

# Public API parity with the umbrella's kernel.pipeline package (QA-while-moving,
# 2026-05-27): consumers (live runner, LEAN/sim adapters) do
# `from kernel.pipeline import InferencePipeline, SellOnlyPipeline, ...`.
from .context import InferenceContext, TickerInferenceContext
from .pipeline import Job, TickerJob, run_parallel
from .pp_inference import InferencePipeline, SellOnlyPipeline

__all__ = [
    "InferenceContext", "TickerInferenceContext",
    "Job", "TickerJob",
    "InferencePipeline", "SellOnlyPipeline",
    "run_parallel",
]
