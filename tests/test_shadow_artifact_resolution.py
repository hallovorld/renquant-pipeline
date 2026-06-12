"""Shadow artifact paths resolve strategy_dir-first (2026-06-11 shadow-dead fix)."""
from __future__ import annotations

from pathlib import Path

from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import (
    _resolve_shadow_artifact_path,
)


def test_resolves_strategy_dir_before_data_root(tmp_path: Path) -> None:
    strategy_dir = tmp_path / "strategy"
    repo = tmp_path / "repo"
    rel = Path("artifacts/prod/panel-ltr.alpha158_fund.json")
    strategy_path = strategy_dir / rel
    repo_path = repo / rel
    strategy_path.parent.mkdir(parents=True)
    repo_path.parent.mkdir(parents=True)
    strategy_path.write_text("strategy", encoding="utf-8")
    repo_path.write_text("repo", encoding="utf-8")

    assert _resolve_shadow_artifact_path(
        rel, strategy_dir=strategy_dir, repo=repo,
    ) == strategy_path


def test_falls_back_to_data_root_when_strategy_artifact_missing(tmp_path: Path) -> None:
    strategy_dir = tmp_path / "strategy"
    repo = tmp_path / "repo"
    rel = Path("artifacts/prod/panel-ltr.alpha158_fund.json")
    repo_path = repo / rel
    repo_path.parent.mkdir(parents=True)
    repo_path.write_text("repo", encoding="utf-8")

    assert _resolve_shadow_artifact_path(
        rel, strategy_dir=strategy_dir, repo=repo,
    ) == repo_path


def test_absolute_shadow_artifact_path_is_preserved(tmp_path: Path) -> None:
    absolute = tmp_path / "absolute-model.json"

    assert _resolve_shadow_artifact_path(
        absolute, strategy_dir=tmp_path / "strategy", repo=tmp_path / "repo",
    ) == absolute
