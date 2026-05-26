"""Candidate selection tasks after alpha admission.

Selection consumes only candidates that have already passed model admission and
buy-floor gates. It must not create alpha eligibility by itself.
"""
from __future__ import annotations

from typing import Any

from renquant_common import Job, Task


class SelectAcceptedCandidatesTask(Task):
    """Pick top accepted candidates without promoting blocked names."""

    def run(self, ctx: Any) -> bool | None:
        cfg = _selection_cfg(ctx)
        accepted = _accepted_candidates(ctx)
        max_new = cfg.get("max_new_positions", cfg.get("max_candidates"))
        if max_new is not None:
            max_new = max(0, int(max_new))
        selected = sorted(
            accepted,
            key=lambda row: (
                float(row.get("rank_score", row.get("panel_score", row.get("score", 0.0)))),
                str(row.get("ticker")),
            ),
            reverse=True,
        )
        if max_new is not None:
            selected = selected[:max_new]
        setattr(ctx, "selected_candidates", selected)
        setattr(ctx, "selection_status", "selected")
        return True


class ValidateSelectionDoesNotPromoteTask(Task):
    """Fail if selection contains names outside the accepted alpha set."""

    def run(self, ctx: Any) -> bool | None:
        accepted = {str(row["ticker"]) for row in _accepted_candidates(ctx)}
        selected = [str(row["ticker"]) for row in _selected_candidates(ctx)]
        promoted = sorted(ticker for ticker in selected if ticker not in accepted)
        if promoted:
            raise ValueError(f"selection promoted non-accepted ticker(s): {promoted}")
        blocked = getattr(ctx, "blocked_by", {}) or {}
        blocked_selected = sorted(ticker for ticker in selected if blocked.get(ticker))
        if blocked_selected:
            raise ValueError(f"selection included blocked ticker(s): {blocked_selected}")
        return True


class SelectionJob(Job):
    """Selection-only job; sizing/QP remains a separate downstream concern."""

    def __init__(self) -> None:
        self._tasks = [SelectAcceptedCandidatesTask(), ValidateSelectionDoesNotPromoteTask()]

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    def should_skip(self, ctx: Any) -> bool:
        cfg = _selection_cfg(ctx)
        return not bool(cfg.get("enabled", True))


def _selection_cfg(ctx: Any) -> dict[str, Any]:
    strategy = getattr(ctx, "strategy_config", {}) or {}
    return (
        strategy.get("ranking", {})
        .get("selection", {})
        or strategy.get("selection", {})
        or {}
    )


def _accepted_candidates(ctx: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in getattr(ctx, "accepted_candidates", []) or []:
        if isinstance(row, dict) and row.get("ticker") and not row.get("blocked_by"):
            out.append(row)
    return out


def _selected_candidates(ctx: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in getattr(ctx, "selected_candidates", []) or []:
        if isinstance(row, dict) and row.get("ticker"):
            out.append(row)
        elif isinstance(row, str):
            out.append({"ticker": row})
    return out
