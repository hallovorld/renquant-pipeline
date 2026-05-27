"""Tests for the lifted orchestration core (functional-lift slice 4).

This slice reconciles the umbrella's duplicate Task/Job/run_parallel onto
``renquant_common`` rather than copying them verbatim. The tests pin:

1. The re-exported primitives ARE ``renquant_common``'s objects — no second
   implementation (RFC Decision item 6, §5.13.5 one-impl rule).
2. ``TickerJob`` is a ``renquant_common.Job`` and short-circuits on a Task
   returning ``False`` (umbrella behavior preserved).
3. ``run_parallel`` runs the job per context, isolates worker faults, derives
   ``max_workers`` / ``timeout`` / ``progress`` defaults from
   ``ticker_ctxs[0].config``, and raises ``ParallelTimeoutError`` on timeout.
4. The verbatim ``atoms`` bind to the re-exported common ``Task``.
"""
from __future__ import annotations

import importlib
import time
from types import SimpleNamespace

import pytest

import renquant_common

pipeline = importlib.import_module("renquant_pipeline.kernel.pipeline.pipeline")


def test_core_primitives_are_common_canonical() -> None:
    """No duplicate impl: re-exported names are common's own objects."""
    assert pipeline.Task is renquant_common.Task
    assert pipeline.Job is renquant_common.Job
    assert pipeline.ParallelTimeoutError is renquant_common.ParallelTimeoutError
    # resolve_workers is re-exported from common's pipeline submodule (not yet
    # in common's top-level __all__) — still the single canonical impl.
    assert pipeline.resolve_workers is renquant_common.pipeline.resolve_workers


def test_tickerjob_is_common_job_subclass() -> None:
    assert issubclass(pipeline.TickerJob, renquant_common.Job)


def test_tickerjob_short_circuits_on_false() -> None:
    calls: list[str] = []

    class T1(pipeline.Task):
        def run(self, ctx):  # noqa: ANN001
            calls.append("t1")
            return False

    class T2(pipeline.Task):
        def run(self, ctx):  # noqa: ANN001
            calls.append("t2")

    class J(pipeline.TickerJob):
        @property
        def tasks(self):
            return [T1(), T2()]

    J().run(SimpleNamespace(ticker="AAPL"))
    assert calls == ["t1"], "task chain must stop after a Task returns False"


def test_run_parallel_runs_per_context_and_isolates_faults() -> None:
    seen: list[str] = []

    class J(pipeline.TickerJob):
        @property
        def tasks(self):
            return []

        def run(self, tc):  # noqa: ANN001
            if tc.ticker == "BOOM":
                raise ValueError("boom")
            seen.append(tc.ticker)

    ctxs = [SimpleNamespace(ticker=t, config={}) for t in ("A", "BOOM", "B")]
    # A worker fault must be logged, not raised — siblings still complete.
    pipeline.run_parallel(ctxs, J(), max_workers=2)
    assert sorted(seen) == ["A", "B"]


def test_run_parallel_empty_is_noop() -> None:
    pipeline.run_parallel([], pipeline.TickerJob())  # must not raise


def test_run_parallel_derives_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_common_rp(ctxs, job, *, max_workers, timeout_seconds, progress_log_seconds):  # noqa: ANN001
        captured.update(
            max_workers=max_workers,
            timeout_seconds=timeout_seconds,
            progress_log_seconds=progress_log_seconds,
        )

    monkeypatch.setattr(pipeline, "_common_run_parallel", fake_common_rp)
    ctxs = [
        SimpleNamespace(
            ticker="A",
            config={
                "parallel_workers": 7,
                "parallel_ticker_timeout_seconds": 99,
                "parallel_progress_log_seconds": 5,
            },
        )
    ]
    pipeline.run_parallel(ctxs, pipeline.TickerJob())
    assert captured == {
        "max_workers": 7,
        "timeout_seconds": 99,
        "progress_log_seconds": 5,
    }


def test_run_parallel_explicit_args_win_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_common_rp(ctxs, job, *, max_workers, timeout_seconds, progress_log_seconds):  # noqa: ANN001
        captured.update(max_workers=max_workers)

    monkeypatch.setattr(pipeline, "_common_run_parallel", fake_common_rp)
    ctxs = [SimpleNamespace(ticker="A", config={"parallel_workers": 7})]
    pipeline.run_parallel(ctxs, pipeline.TickerJob(), max_workers=2)
    assert captured["max_workers"] == 2


def test_run_parallel_timeout_raises() -> None:
    class Slow(pipeline.TickerJob):
        @property
        def tasks(self):
            return []

        def run(self, tc):  # noqa: ANN001
            time.sleep(0.5)

    ctxs = [SimpleNamespace(ticker=t, config={}) for t in ("A", "B")]
    with pytest.raises(pipeline.ParallelTimeoutError):
        pipeline.run_parallel(
            ctxs, Slow(), max_workers=1, timeout_seconds=0.05, progress_log_seconds=1000
        )


def test_atoms_bind_to_common_task() -> None:
    """Verbatim atoms must resolve `from ..pipeline import Task` to common.Task."""
    atoms = importlib.import_module("renquant_pipeline.kernel.pipeline.atoms")
    for cls_name in (
        "IsFiniteGuardTask",
        "CopyFieldTask",
        "LogSummaryTask",
        "BuildVectorFromMappingTask",
    ):
        cls = getattr(atoms, cls_name)
        assert issubclass(cls, renquant_common.Task), f"{cls_name} not bound to common.Task"
