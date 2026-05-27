"""Task / Job / TickerJob ABCs and run_parallel — composed on renquant_common.

Per RFC §"Decisions Already Fixed" item 6 and §"Backfill Plan" functional-lift,
the runtime decision pipeline does NOT reinvent orchestration primitives. The
canonical ``Task`` / ``Job`` / ``run_parallel`` / ``ParallelTimeoutError`` /
``resolve_workers`` live in :mod:`renquant_common`; this module re-exports them
and adds only the two pipeline-domain extensions the umbrella kernel needs:

* ``TickerJob`` — a :class:`renquant_common.Job` whose short-circuit debug log
  is labeled by ``tc.ticker`` (per-ticker pipeline stage). Behaviorally
  identical to ``Job`` otherwise.
* ``run_parallel(ticker_ctxs, job, ...)`` — resolves ``max_workers`` /
  ``timeout_seconds`` / ``progress_log_seconds`` defaults from
  ``ticker_ctxs[0].config`` (the umbrella's domain-config convention) before
  delegating to the canonical :func:`renquant_common.run_parallel` executor.

This collapses the duplicate executor loop the bootstrap shipped in the
umbrella ``kernel/pipeline/pipeline.py`` — a §5.13.5 ("one business decision =
one function") violation living across the repo boundary. Decisions are
unchanged: ``renquant_common.run_parallel`` labels per-ticker contexts via
``ctx.ticker`` exactly as before; only cosmetic log strings / thread names and
the (un-read) ``ParallelTimeoutError`` attribute name differ.
"""
from __future__ import annotations

import logging

from renquant_common import (
    Job,
    ParallelTimeoutError,
    Task,
)
from renquant_common import run_parallel as _common_run_parallel

# `resolve_workers` is a public helper in common's pipeline submodule but is not
# yet in common's top-level ``__all__``. Re-export from the submodule (single
# implementation, no duplicate) rather than forcing a minor common bump that
# would fall outside pipeline's pinned ``renquant-common>=0.2,<0.3`` range.
# Promoting it to the package API is a future additive common change.
from renquant_common.pipeline import resolve_workers

from renquant_pipeline.context import InferenceContext, TickerInferenceContext

log = logging.getLogger("kernel.pipeline")

__all__ = [
    "Task",
    "Job",
    "TickerJob",
    "run_parallel",
    "ParallelTimeoutError",
    "resolve_workers",
]


class TickerJob(Job):
    """Per-ticker pipeline stage — reads/writes ``TickerInferenceContext``.

    Behaviorally identical to :class:`renquant_common.Job`; ``run`` is
    overridden only to label the short-circuit debug log with the ticker so
    per-ticker traces stay readable.
    """

    @property
    def tasks(self) -> "list[Task]":
        return []

    def run(self, tc: TickerInferenceContext) -> None:
        for task in self.tasks:
            if task.run(tc) is False:
                log.debug(
                    "[%s|%s] chain stopped by %s",
                    tc.ticker,
                    type(self).__name__,
                    task.name,
                )
                return


def run_parallel(
    ticker_ctxs: "list[TickerInferenceContext]",
    job: TickerJob,
    max_workers: "int | None" = None,
    timeout_seconds: "float | None" = None,
    progress_log_seconds: "float | None" = None,
) -> None:
    """Resolve per-ticker parallel defaults from config, then delegate to common.

    The executor loop (worker pool, wall-clock timeout, progress logging, and
    per-item fault isolation) is the canonical
    :func:`renquant_common.run_parallel`. This wrapper preserves the umbrella's
    behavior of deriving the three defaults from ``ticker_ctxs[0].config`` when
    they are not passed explicitly.
    """
    if not ticker_ctxs:
        return
    cfg = getattr(ticker_ctxs[0], "config", None)
    if isinstance(cfg, dict):
        if max_workers is None:
            max_workers = cfg.get("parallel_workers")
        if timeout_seconds is None:
            timeout_seconds = cfg.get("parallel_ticker_timeout_seconds")
        if progress_log_seconds is None:
            progress_log_seconds = cfg.get("parallel_progress_log_seconds")
    _common_run_parallel(
        ticker_ctxs,
        job,
        max_workers=max_workers,
        timeout_seconds=timeout_seconds,
        progress_log_seconds=progress_log_seconds,
    )
