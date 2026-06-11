"""Shadow artifact paths resolve strategy_dir-first (2026-06-11 shadow-dead fix).

Pre-fix ApplyShadowScoringTask resolved relative artifact_path ONLY against
data_root(), so the post-PatchTST-promotion shadow (whose artifact lives under
<strategy_dir>/artifacts/prod/) failed to load every run — the
primary-vs-previous monitor was silently off. Source-level contract pins the
strategy_dir-first, data_root-fallback order.
"""
from __future__ import annotations

from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent
       / "src/renquant_pipeline/kernel/panel_pipeline/shadow_scoring.py").read_text()


def test_resolves_strategy_dir_first() -> None:
    assert 'ctx.config.get("_strategy_dir")' in SRC
    assert "candidates.append(Path(strategy_dir) / p)" in SRC


def test_keeps_data_root_fallback() -> None:
    assert "candidates.append(repo / p)" in SRC
    assert "next((c for c in candidates if c.exists()), candidates[-1])" in SRC
