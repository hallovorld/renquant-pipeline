"""Shared umbrella data-root resolver for pipeline scorer modules.

Today's daily run failed because `Path(__file__).resolve().parents[4]` in
multiple scorer modules resolved to the renquant-pipeline checkout (not
the umbrella) under the multirepo daily routing introduced by codex PR #31.

This module is the §7.5 single source of truth. All scorer modules
(`job_panel_scoring.py`, `hf_patchtst_scorer.py`, `patchtst_scorer.py`,
`model_registry.py`, `shadow_scoring.py`) MUST route umbrella-root
resolution through ``data_root()`` instead of computing ``parents[4]``
locally. Mixing local resolvers in subset of modules re-creates the
silent prod break this hotfix was opened for.

Resolution order:
  1. ``RENQUANT_DATA_ROOT`` env var (canonical override). Same contract
     as codex PR renquant-model#22. Must exist AND contain the sentinel;
     misconfigured values raise RuntimeError.
  2. Sibling `<pipeline-parent>/RenQuant` if it has the sentinel.
  3. ``~/git/github/RenQuant`` convention if it has the sentinel.
  4. Legacy ``parents[4]`` — only valid when this file is loaded from
     within an umbrella checkout (rollback via RQ_DAILY_RUNNER=umbrella).

The sentinel is ``data/sec_fundamentals_daily.parquet`` because that file
is required for every prod daily run; if it's missing, no panel scoring
can succeed anyway, so failing fast at root-resolution time is correct.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# Pinned sentinel — production-required artifact path. If this file moves,
# update tests/test_data_root_resolver.py accordingly (the change is a
# breaking infrastructure change, not a silent rename).
SENTINEL_RELATIVE = Path("data") / "sec_fundamentals_daily.parquet"


def _valid(root: Path) -> bool:
    return (root / SENTINEL_RELATIVE).exists()


def _resolve() -> Path:
    raw = os.environ.get("RENQUANT_DATA_ROOT")
    if raw:
        cand = Path(raw).expanduser().resolve()
        if not cand.exists():
            raise RuntimeError(
                f"RENQUANT_DATA_ROOT={raw!r} does not exist; refusing to start "
                f"with a misconfigured data root."
            )
        if not _valid(cand):
            raise RuntimeError(
                f"RENQUANT_DATA_ROOT={raw!r} exists but is missing sentinel "
                f"{SENTINEL_RELATIVE}. Set it to the umbrella RenQuant checkout root."
            )
        return cand

    # This module's parents[4] = renquant-pipeline checkout root.
    # Caller: src/renquant_pipeline/kernel/panel_pipeline/_data_root.py
    # parents: [0]=panel_pipeline [1]=kernel [2]=renquant_pipeline [3]=src [4]=<repo>
    pkg_root = Path(__file__).resolve().parents[4]
    sibling = (pkg_root.parent / "RenQuant").resolve()
    if _valid(sibling):
        return sibling
    home_default = (Path.home() / "git" / "github" / "RenQuant").resolve()
    if _valid(home_default):
        return home_default
    if _valid(pkg_root):       # umbrella rollback
        return pkg_root
    raise RuntimeError(
        f"unable to resolve umbrella data root. Tried: "
        f"RENQUANT_DATA_ROOT(unset), {sibling}, {home_default}, {pkg_root}. "
        f"None contain {SENTINEL_RELATIVE}. Set RENQUANT_DATA_ROOT explicitly."
    )


@lru_cache(maxsize=1)
def data_root() -> Path:
    """Return the umbrella RenQuant checkout root. Cached per process.

    Raises RuntimeError if no valid root can be located. Callers should not
    catch this — it indicates a misconfigured deployment that must be fixed
    before any scoring path can succeed.
    """
    return _resolve()


def _reset_cache_for_tests() -> None:
    """Test-only: clear lru_cache so env-var test parametrization works."""
    data_root.cache_clear()  # type: ignore[attr-defined]
