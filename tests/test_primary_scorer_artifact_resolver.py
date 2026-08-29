"""PRIMARY scorer + global calibrator resolve config refs through the ONE
artifact-resolution authority (orch#1066 option a').

Pre-fix (origin/main 76ab129, job_panel_scoring.py:915-928 / 943-949 /
3183-3184) the primary loader and the global calibrator joined a relative
ref onto ``_strategy_dir`` ONLY, while blend components
(``blend_scorer._resolve_component_path`` → ``artifact_resolver``) and every
preflight check fell back to the repo root. Same ref string, two answers.

Invariants pinned here:
- strategy_dir copy present → the strategy_dir path, byte-identical to the
  pre-fix join (unchanged behaviour; this is the production shape);
- only a repo_root copy present → the repo_root path (the fix), and it is
  the SAME path the blend components resolve for that ref;
- neither present → the strategy_dir candidate, i.e. the pre-fix path, so
  the kind loader raises the same error class/text and the scoring
  contract fails closed with the same reason (``panel_scorer_load_failed``);
- production-shaped blend config → the blend component path list AND the
  component-0 anchor are unchanged vs main;
- global calibration follows the same precedence and keeps its
  ``calibrator_load_failed`` miss semantics.

Collected by ``make test`` (``python -m pytest -q`` from the repo root — the
CI "Test" step); no workflow edit is needed for this file to run.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.artifact_resolver import resolve_artifact
from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as jps
from renquant_pipeline.kernel.panel_pipeline.blend_scorer import (
    _resolve_component_path,
)
from renquant_pipeline.kernel.panel_pipeline.global_calibrator import (
    GlobalPanelCalibration,
)
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    LoadGlobalCalibrationTask,
    LoadScorerTask,
    _locate_config_artifact,
)
from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
from renquant_pipeline.kernel.selection import CandidateResult

# Literal refs from the pinned strategy-104 configs (d3c8026, configs/):
# strategy_config.shadow.json (PRIMARY hf_patchtst + global_calibration) and
# strategy_config.json (PRIMARY blend). Copied as strings on purpose — the
# tests must never read the operator's disk.
SHADOW_PRIMARY_REF = (
    "artifacts/patchtst_shadow/pt07_strict_trainfit_embargo60_20260522/"
    "seed_44/hf_patchtst_all_seed44_model.pt"
)
SHADOW_CALIBRATION_REF = (
    "artifacts/shadow/panel-rank-calibration."
    "hf_patchtst_seed44_trainfit_20230103_20240409.json"
)
PROD_COMPONENT_REFS = [
    "artifacts/prod/panel-ltr.alpha158_fund.json",          # classic xgb leg
    "artifacts/momentum/momentum_artifact_ledger.jsonl",    # momentum_residual
]
PROD_OTHER_REFS = [
    "artifacts/shadow/panel-clf.top-decile.fwd60.json",     # shadow_models[0]
    "artifacts/momentum_fast/momentum_artifact_ledger.jsonl",
    "artifacts/prod/panel-rank-calibration.json",           # gc (disabled)
    "artifacts/prod/ngboost-head.alpha158_fund.json",       # ngboost (disabled)
]


def _legacy_join(strategy_dir: Path, ref: str) -> Path:
    """The pre-fix rule, verbatim: ``Path(strategy_dir) / ref`` for a
    relative ref (job_panel_scoring.py:924-928 @ origin/main 76ab129)."""
    p = Path(ref)
    return p if p.is_absolute() else Path(strategy_dir) / p


def _put(root: Path, ref: str, body: bytes) -> Path:
    p = root / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return p


@pytest.fixture()
def layout(tmp_path):
    """``<repo_root>/backtesting/renquant_104`` — the umbrella convention the
    resolver's ``default_repo_root`` encodes (artifact_resolver.py:42-48)."""
    repo_root = tmp_path.resolve()
    strategy_dir = repo_root / "backtesting" / "renquant_104"
    strategy_dir.mkdir(parents=True)
    return repo_root, strategy_dir


def _ctx(strategy_dir: Path | None, panel_cfg: dict) -> InferenceContext:
    config: dict = {"ranking": {"panel_scoring": panel_cfg}}
    if strategy_dir is not None:
        config["_strategy_dir"] = str(strategy_dir)
    return InferenceContext(
        config=config,
        today=dt.date(2026, 8, 29),
        candidates=[
            CandidateResult("AAPL", 0.1, 0.1, 0.0),
            CandidateResult("MSFT", 0.1, 0.1, 0.0),
        ],
        holdings={},
    )


def _primary_cfg(ref: str, kind: str = "hf_patchtst", **extra) -> dict:
    cfg = {"enabled": True, "kind": kind, "artifact_path": ref}
    cfg.update(extra)
    return cfg


# ── precedence: the helper is the blend components' rule ─────────────────────


class TestPrimaryResolution:

    def test_strategy_dir_copy_wins_and_is_byte_identical_to_legacy(self, layout):
        repo_root, strategy_dir = layout
        _put(strategy_dir, SHADOW_PRIMARY_REF, b"strategy")
        _put(repo_root, SHADOW_PRIMARY_REF, b"root")
        ctx = _ctx(strategy_dir, _primary_cfg(SHADOW_PRIMARY_REF))
        got = LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"])
        assert got == _legacy_join(strategy_dir, SHADOW_PRIMARY_REF)
        assert str(got) == str(_legacy_join(strategy_dir, SHADOW_PRIMARY_REF))
        assert got.read_bytes() == b"strategy"

    def test_repo_root_fallback_is_the_fix_and_matches_blend_components(self, layout):
        repo_root, strategy_dir = layout
        _put(repo_root, SHADOW_PRIMARY_REF, b"root-only")
        ctx = _ctx(strategy_dir, _primary_cfg(SHADOW_PRIMARY_REF))
        got = LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"])
        assert got == repo_root / SHADOW_PRIMARY_REF
        assert got.is_file() and got.read_bytes() == b"root-only"
        # pre-fix this was the (missing) strategy_dir join
        assert got != _legacy_join(strategy_dir, SHADOW_PRIMARY_REF)
        # SAME answer the blend components get for the same ref string
        assert got.resolve() == _resolve_component_path(SHADOW_PRIMARY_REF, strategy_dir).resolve()
        assert got.resolve() == resolve_artifact(SHADOW_PRIMARY_REF, strategy_dir=strategy_dir).path

    def test_neither_copy_reports_the_strategy_dir_candidate_as_before(self, layout):
        repo_root, strategy_dir = layout
        ctx = _ctx(strategy_dir, _primary_cfg(SHADOW_PRIMARY_REF))
        got = LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"])
        assert got == _legacy_join(strategy_dir, SHADOW_PRIMARY_REF)
        assert not got.exists()

    def test_absolute_ref_untouched(self, layout, tmp_path):
        repo_root, strategy_dir = layout
        abs_ref = str(tmp_path / "elsewhere" / "model.pt")
        ctx = _ctx(strategy_dir, _primary_cfg(abs_ref))
        got = LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"])
        assert got == Path(abs_ref)

    def test_no_strategy_dir_reduces_to_bare_ref(self):
        ctx = _ctx(None, _primary_cfg(SHADOW_PRIMARY_REF))
        got = LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"])
        assert got == Path(SHADOW_PRIMARY_REF)          # pre-fix: `Path(artifact_path)`

    def test_no_artifact_path_still_none(self, layout):
        repo_root, strategy_dir = layout
        ctx = _ctx(strategy_dir, {"enabled": True, "kind": "xgb"})
        assert LoadScorerTask._resolve_artifact_path(ctx, ctx.config["ranking"]["panel_scoring"]) is None

    def test_scorer_metadata_ref_takes_precedence_and_resolves_same_way(self, layout):
        repo_root, strategy_dir = layout
        meta_ref = "artifacts/from_metadata/model.json"
        _put(repo_root, meta_ref, b"{}")
        ctx = _ctx(strategy_dir, _primary_cfg(SHADOW_PRIMARY_REF))
        scorer = SimpleNamespace(metadata={"artifact_path": meta_ref})
        got = LoadScorerTask._resolve_artifact_path(
            ctx, ctx.config["ranking"]["panel_scoring"], scorer)
        assert got == repo_root / meta_ref


# ── the load site: fresh load + fail-closed contract ─────────────────────────


class _StubHandler:
    kind = "xgb"
    requires_history = False

    def __init__(self):
        self.seen: list[Path] = []

    def scorer_loader(self, artifact_path, config):
        self.seen.append(Path(artifact_path))
        return SimpleNamespace(
            kind="xgb", feature_cols=["f1"], metadata={}, requires_history=False,
        )


class TestLoadSite:

    def test_fresh_load_receives_repo_root_path(self, layout, monkeypatch):
        repo_root, strategy_dir = layout
        _put(repo_root, "artifacts/only_root/model.pt", b"weights")
        stub = _StubHandler()
        monkeypatch.setattr(registry, "get", lambda kind: stub)
        ctx = _ctx(strategy_dir, _primary_cfg(
            "artifacts/only_root/model.pt", kind="xgb", strict_config_consistency=False))
        LoadScorerTask().run(ctx)
        assert stub.seen == [repo_root / "artifacts/only_root/model.pt"]
        assert ctx._active_panel_artifact_path == str(repo_root / "artifacts/only_root/model.pt")
        assert not getattr(ctx, "_panel_scoring_contract_failed", False)
        assert len(ctx.candidates) == 2

    def test_fresh_load_receives_strategy_dir_path_when_present(self, layout, monkeypatch):
        repo_root, strategy_dir = layout
        _put(strategy_dir, "artifacts/both/model.pt", b"s")
        _put(repo_root, "artifacts/both/model.pt", b"r")
        stub = _StubHandler()
        monkeypatch.setattr(registry, "get", lambda kind: stub)
        ctx = _ctx(strategy_dir, _primary_cfg(
            "artifacts/both/model.pt", kind="xgb", strict_config_consistency=False))
        LoadScorerTask().run(ctx)
        assert stub.seen == [_legacy_join(strategy_dir, "artifacts/both/model.pt")]

    def test_missing_everywhere_fails_closed_with_same_error_and_reason(self, layout, caplog):
        repo_root, strategy_dir = layout
        panel_cfg = _primary_cfg(SHADOW_PRIMARY_REF, kind="xgb")
        ctx = _ctx(strategy_dir, panel_cfg)

        # (1) the path handed to the kind loader is the pre-fix path …
        new_path = LoadScorerTask._resolve_artifact_path(ctx, panel_cfg)
        legacy_path = _legacy_join(strategy_dir, SHADOW_PRIMARY_REF)
        assert new_path == legacy_path

        # (2) … so the real xgb loader raises the same class + text as today
        def _raise(p):
            try:
                registry.get("xgb").scorer_loader(p, ctx.config)
            except Exception as exc:  # noqa: BLE001
                return exc
            raise AssertionError("loader unexpectedly succeeded on a missing file")

        e_new, e_old = _raise(new_path), _raise(legacy_path)
        assert type(e_new) is type(e_old)
        assert str(e_new) == str(e_old)
        assert isinstance(e_new, FileNotFoundError)

        # (3) … and the scoring contract fails closed exactly as before.
        with caplog.at_level(logging.ERROR, logger=jps.log.name):
            result = LoadScorerTask().run(ctx)
        assert result is False
        assert ctx._panel_scoring_fail_reason == "panel_scorer_load_failed"
        assert ctx._panel_scoring_contract_failed is True
        assert ctx.candidates == [] and ctx.skip_buys is True
        assert ctx.counters["panel_scoring_fail_closed"] == 2
        assert ctx._blocked_by_ticker == {  # noqa: SLF001
            "AAPL": "panel_scorer_load_failed", "MSFT": "panel_scorer_load_failed",
        }
        assert any(
            "failed to load xgb artifact" in r.getMessage() and str(legacy_path) in r.getMessage()
            for r in caplog.records
        )

    def test_preloaded_scorer_branch_stamps_repo_root_path(self, layout):
        """Adapter/LEAN branch (scorer pre-loaded): the path only anchors the
        consistency gate + trace stamp; it now follows the same precedence."""
        repo_root, strategy_dir = layout
        _put(repo_root, SHADOW_PRIMARY_REF, b"weights")
        ctx = _ctx(strategy_dir, _primary_cfg(
            SHADOW_PRIMARY_REF, strict_config_consistency=False))
        ctx._panel_scorer = SimpleNamespace(  # noqa: SLF001
            kind="hf_patchtst", feature_cols=["f1"], metadata={}, requires_history=True)
        LoadScorerTask().run(ctx)
        assert ctx._active_panel_artifact_path == str(repo_root / SHADOW_PRIMARY_REF)


# ── production shape: blend PRIMARY — nothing moves ──────────────────────────


class TestProductionBlendUnchanged:

    @pytest.fixture()
    def prod_layout(self, layout):
        repo_root, strategy_dir = layout
        for ref in PROD_COMPONENT_REFS + PROD_OTHER_REFS:
            _put(strategy_dir, ref, b"{}")          # production: every ref lives in the bundle
        return repo_root, strategy_dir

    @staticmethod
    def _prod_cfg() -> dict:
        return {
            "enabled": True,
            "kind": "blend",
            "components": [
                {"artifact_path": PROD_COMPONENT_REFS[0],
                 "expected_content_sha256": "x", "expected_config_fingerprint": "y"},
                {"kind": "momentum_residual", "artifact_path": PROD_COMPONENT_REFS[1],
                 "expected_config_fingerprint": "z"},
            ],
            "global_calibration": {"enabled": False, "artifact_path": PROD_OTHER_REFS[2]},
            "ngboost": {"enabled": False, "artifact_path": PROD_OTHER_REFS[3]},
        }

    def test_blend_component_path_list_is_the_strategy_dir_list(self, prod_layout):
        """The resolver's answer for the production components == main's
        list (strategy_dir join). Asserted against the resolver output, not
        by loading the models."""
        repo_root, strategy_dir = prod_layout
        blend_paths = [
            _resolve_component_path(ref, strategy_dir) for ref in PROD_COMPONENT_REFS
        ]
        legacy = [_legacy_join(strategy_dir, ref).resolve() for ref in PROD_COMPONENT_REFS]
        assert blend_paths == legacy
        assert all(p.source == "strategy_dir" for p in (
            resolve_artifact(ref, strategy_dir=strategy_dir) for ref in PROD_COMPONENT_REFS
        ))

    def test_blend_component0_anchor_unchanged(self, prod_layout):
        repo_root, strategy_dir = prod_layout
        cfg = self._prod_cfg()
        ctx = _ctx(strategy_dir, cfg)
        # kind=blend carries no top-level artifact_path → anchor = component 0
        assert LoadScorerTask._resolve_artifact_path(ctx, cfg) is None
        anchor = LoadScorerTask._blend_component0_path(ctx, cfg)
        assert anchor == _legacy_join(strategy_dir, PROD_COMPONENT_REFS[0])
        assert str(anchor) == str(_legacy_join(strategy_dir, PROD_COMPONENT_REFS[0]))
        assert anchor.resolve() == _resolve_component_path(PROD_COMPONENT_REFS[0], strategy_dir)

    def test_every_production_ref_resolves_as_before(self, prod_layout):
        repo_root, strategy_dir = prod_layout
        for ref in PROD_COMPONENT_REFS + PROD_OTHER_REFS:
            assert _locate_config_artifact(strategy_dir, ref) == _legacy_join(strategy_dir, ref)
            assert _locate_config_artifact(str(strategy_dir), ref) == _legacy_join(strategy_dir, ref)

    def test_helper_precedence_equals_resolver_precedence(self, layout):
        """One authority: for every layout the helper returns the resolver's
        first existing candidate (strategy_dir before repo_root)."""
        repo_root, strategy_dir = layout
        ref = "artifacts/x/y.json"
        assert _locate_config_artifact(strategy_dir, ref) == strategy_dir / ref  # miss → canonical
        _put(repo_root, ref, b"r")
        assert _locate_config_artifact(strategy_dir, ref) == repo_root / ref
        assert _locate_config_artifact(strategy_dir, ref).resolve() == resolve_artifact(
            ref, strategy_dir=strategy_dir).path
        _put(strategy_dir, ref, b"s")
        assert _locate_config_artifact(strategy_dir, ref) == strategy_dir / ref
        assert _locate_config_artifact(strategy_dir, ref).resolve() == resolve_artifact(
            ref, strategy_dir=strategy_dir).path


# ── global calibration: same authority, same miss semantics ──────────────────


class TestGlobalCalibrationResolution:

    @pytest.fixture()
    def stub_loader(self, monkeypatch):
        seen: list[Path] = []

        def _load(cls, path):
            p = Path(path)
            seen.append(p)
            if not p.is_file():
                raise FileNotFoundError(str(p))
            return SimpleNamespace(metadata={"pool_ic": 0.05})

        monkeypatch.setattr(GlobalPanelCalibration, "load", classmethod(_load))
        monkeypatch.setattr(jps, "_assert_calibrator_matches_scorer",
                            lambda ctx, cal, path, *, strict: None)
        return seen

    @staticmethod
    def _gc_cfg(ref: str) -> dict:
        return {"enabled": True, "kind": "hf_patchtst", "artifact_path": SHADOW_PRIMARY_REF,
                "global_calibration": {"enabled": True, "artifact_path": ref}}

    def test_pooled_calibrator_strategy_dir_first(self, layout, stub_loader):
        repo_root, strategy_dir = layout
        _put(strategy_dir, SHADOW_CALIBRATION_REF, b"{}")
        _put(repo_root, SHADOW_CALIBRATION_REF, b"{}")
        ctx = _ctx(strategy_dir, self._gc_cfg(SHADOW_CALIBRATION_REF))
        LoadGlobalCalibrationTask().run(ctx)
        assert stub_loader == [_legacy_join(strategy_dir, SHADOW_CALIBRATION_REF)]
        assert ctx._global_calibrator is not None

    def test_pooled_calibrator_repo_root_fallback(self, layout, stub_loader):
        repo_root, strategy_dir = layout
        _put(repo_root, SHADOW_CALIBRATION_REF, b"{}")
        ctx = _ctx(strategy_dir, self._gc_cfg(SHADOW_CALIBRATION_REF))
        LoadGlobalCalibrationTask().run(ctx)
        assert stub_loader == [repo_root / SHADOW_CALIBRATION_REF]
        assert ctx._global_calibrator is not None
        assert getattr(ctx, "_global_calibrator_missing_reason", None) is None

    def test_pooled_calibrator_miss_keeps_reason_and_path(self, layout, stub_loader, caplog):
        repo_root, strategy_dir = layout
        ctx = _ctx(strategy_dir, self._gc_cfg(SHADOW_CALIBRATION_REF))
        with caplog.at_level(logging.WARNING, logger=jps.log.name):
            LoadGlobalCalibrationTask().run(ctx)
        legacy = _legacy_join(strategy_dir, SHADOW_CALIBRATION_REF)
        assert stub_loader == [legacy]
        assert ctx._global_calibrator is None
        assert ctx._global_calibrator_missing_reason == "calibrator_load_failed"
        assert any(str(legacy) in r.getMessage() for r in caplog.records)

    def test_per_regime_explicit_map_follows_same_precedence(self, layout, stub_loader):
        repo_root, strategy_dir = layout
        _put(strategy_dir, SHADOW_CALIBRATION_REF, b"{}")
        _put(repo_root, "artifacts/cal/bear.json", b"{}")
        cfg = self._gc_cfg(SHADOW_CALIBRATION_REF)
        cfg["calibrator_per_regime"] = {"BEAR": "artifacts/cal/bear.json"}
        ctx = _ctx(strategy_dir, cfg)
        LoadGlobalCalibrationTask().run(ctx)
        assert stub_loader == [
            _legacy_join(strategy_dir, SHADOW_CALIBRATION_REF),
            repo_root / "artifacts/cal/bear.json",
        ]
        assert set(ctx._regime_calibrators) == {"BEAR"}
        assert ctx._regime_calibrator_paths == {"BEAR": str(repo_root / "artifacts/cal/bear.json")}

    def test_per_regime_missing_still_raises_same_message(self, layout, stub_loader):
        repo_root, strategy_dir = layout
        _put(strategy_dir, SHADOW_CALIBRATION_REF, b"{}")
        cfg = self._gc_cfg(SHADOW_CALIBRATION_REF)
        cfg["calibrator_per_regime"] = {"BEAR": "artifacts/cal/none.json"}
        ctx = _ctx(strategy_dir, cfg)
        with pytest.raises(FileNotFoundError) as ei:
            LoadGlobalCalibrationTask().run(ctx)
        assert "calibrator_per_regime[BEAR] artifact not found" in str(ei.value)
        assert str(_legacy_join(strategy_dir, "artifacts/cal/none.json")) in str(ei.value)
