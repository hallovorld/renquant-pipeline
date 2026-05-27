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
"""
from __future__ import annotations

__all__: list[str] = []
