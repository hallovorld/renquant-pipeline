"""ArtifactResolver tests — single path-resolution authority (eng plan §III.5).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.5;
incident #2 = renquant-pipeline PR #114 (shadow artifact dead a week from
primary/shadow resolving the same ref against different roots).

Invariants pinned:
- fixed candidate order: absolute → strategy_dir → repo_root
- strategy_dir wins when the ref exists in both places
- fail-closed: resolve_artifact raises FileNotFoundError naming every
  candidate tried
- sha256 prefix is stable and content-derived (run-fingerprint input)
- locate_artifact never raises; missing ref reports the strategy_dir
  candidate
- preflight._resolve_artifact_path delegates here (repo-root fallback now
  visible to preflight)
"""
from __future__ import annotations

import hashlib

import pytest

from renquant_pipeline.kernel.artifact_resolver import (
    ResolvedArtifact,
    default_repo_root,
    locate_artifact,
    resolve_artifact,
)


@pytest.fixture()
def layout(tmp_path):
    """repo_root/backtesting/renquant_104 strategy-dir convention."""
    repo_root = tmp_path
    strategy_dir = repo_root / "backtesting" / "renquant_104"
    strategy_dir.mkdir(parents=True)
    return repo_root, strategy_dir


class TestResolveArtifact:

    def test_strategy_dir_first(self, layout):
        repo_root, strategy_dir = layout
        ref = "artifacts/prod/panel-ltr.json"
        for root, body in ((strategy_dir, b"strategy"), (repo_root, b"root")):
            (root / ref).parent.mkdir(parents=True, exist_ok=True)
            (root / ref).write_bytes(body)
        got = resolve_artifact(ref, strategy_dir=strategy_dir, repo_root=repo_root)
        assert got.source == "strategy_dir"
        assert got.path.read_bytes() == b"strategy"

    def test_repo_root_fallback(self, layout):
        repo_root, strategy_dir = layout
        ref = "artifacts/only_at_root.json"
        (repo_root / ref).parent.mkdir(parents=True)
        (repo_root / ref).write_bytes(b"{}")
        got = resolve_artifact(ref, strategy_dir=strategy_dir, repo_root=repo_root)
        assert got.source == "repo_root"

    def test_absolute_ref(self, layout):
        repo_root, strategy_dir = layout
        f = repo_root / "abs.json"
        f.write_bytes(b"{}")
        got = resolve_artifact(str(f), strategy_dir=strategy_dir, repo_root=repo_root)
        assert got.source == "absolute"
        assert got.path == f.resolve()

    def test_fail_closed_lists_candidates(self, layout):
        repo_root, strategy_dir = layout
        with pytest.raises(FileNotFoundError) as exc:
            resolve_artifact("nope/missing.json",
                             strategy_dir=strategy_dir, repo_root=repo_root)
        msg = str(exc.value)
        assert "fail-closed" in msg
        assert str(strategy_dir) in msg and str(repo_root) in msg

    def test_sha256_is_content_digest(self, layout):
        repo_root, strategy_dir = layout
        f = strategy_dir / "m.json"
        f.write_bytes(b"payload")
        got = resolve_artifact("m.json", strategy_dir=strategy_dir,
                               repo_root=repo_root)
        assert got.sha256 == hashlib.sha256(b"payload").hexdigest()[:16]

    def test_directory_does_not_satisfy(self, layout):
        # A directory at the candidate path must not be "resolved" — loads
        # need files; fall through (here: to fail-closed).
        repo_root, strategy_dir = layout
        (strategy_dir / "artifacts").mkdir()
        with pytest.raises(FileNotFoundError):
            resolve_artifact("artifacts", strategy_dir=strategy_dir,
                             repo_root=repo_root)

    def test_default_repo_root_convention(self, layout):
        repo_root, strategy_dir = layout
        assert default_repo_root(strategy_dir) == repo_root
        ref = "artifacts/x.json"
        (repo_root / ref).parent.mkdir(parents=True)
        (repo_root / ref).write_bytes(b"{}")
        got = resolve_artifact(ref, strategy_dir=strategy_dir)  # repo_root derived
        assert got.source == "repo_root"

    def test_returns_named_tuple_with_ref(self, layout):
        repo_root, strategy_dir = layout
        (strategy_dir / "a.json").write_bytes(b"{}")
        got = resolve_artifact("a.json", strategy_dir=strategy_dir,
                               repo_root=repo_root)
        assert isinstance(got, ResolvedArtifact)
        assert got.ref == "a.json"


class TestLocateArtifact:

    def test_never_raises_reports_strategy_dir_candidate(self, layout):
        repo_root, strategy_dir = layout
        p = locate_artifact("missing.json", strategy_dir=strategy_dir,
                            repo_root=repo_root)
        assert p == strategy_dir / "missing.json"

    def test_finds_repo_root_fallback(self, layout):
        repo_root, strategy_dir = layout
        (repo_root / "f.json").write_bytes(b"{}")
        p = locate_artifact("f.json", strategy_dir=strategy_dir,
                            repo_root=repo_root)
        assert p == repo_root / "f.json"


class TestPreflightDelegation:

    def test_preflight_resolver_uses_authority(self, layout):
        from renquant_pipeline.kernel.preflight import _resolve_artifact_path

        repo_root, strategy_dir = layout
        # Exists only at repo root: pre-migration preflight would report
        # the (missing) strategy_dir path; post-migration it must find it.
        (repo_root / "artifacts").mkdir()
        (repo_root / "artifacts" / "panel.json").write_bytes(b"{}")
        p = _resolve_artifact_path(strategy_dir, "artifacts/panel.json")
        assert p == repo_root / "artifacts" / "panel.json"

    def test_preflight_resolver_missing_keeps_strategy_dir_report(self, layout):
        from renquant_pipeline.kernel.preflight import _resolve_artifact_path

        repo_root, strategy_dir = layout
        p = _resolve_artifact_path(strategy_dir, "artifacts/none.json")
        assert p == strategy_dir / "artifacts" / "none.json"
