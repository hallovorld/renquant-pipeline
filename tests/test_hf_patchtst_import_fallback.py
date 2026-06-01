"""Behavioral regression tests for HFPatchTSTPanelScorer.load() import paths.

Codex PR #7 review #2: previous meta-test only exercised the exception
logic inline. These tests monkeypatch sys.modules + builtins.__import__
and actually call HFPatchTSTPanelScorer.load() to verify:

  1. Package not installed at all (exc.name == "renquant_model_patchtst")
     → fall back to file-import branch.
  2. Stub package installed without hf_trainer submodule
     (exc.name == "renquant_model_patchtst.hf_trainer")
     → fall back to file-import branch.
  3. hf_trainer present but internally broken (e.g. transformers missing)
     (exc.name == "transformers")
     → propagate; do NOT fall back.

Detection strategy: when the fallback file-import branch is reached AND
the legacy script doesn't exist on disk, the scorer raises:

  ImportError: "renquant_model_patchtst.hf_trainer.HFPatchTSTRanker
                unavailable AND legacy <path> missing."

We catch that specific message to confirm fallback was entered. Conversely,
the propagation test asserts that a ModuleNotFoundError(name='transformers')
surfaces unchanged (not converted to the fallback ImportError).
"""
from __future__ import annotations

import sys
import types
import builtins
import pytest


@pytest.fixture
def isolated_sys_modules():
    """Snapshot + restore renquant_model_patchtst-related entries so tests
    can install/uninstall fakes without polluting other tests."""
    saved = {}
    for key in list(sys.modules):
        if key == "renquant_model_patchtst" or key.startswith("renquant_model_patchtst."):
            saved[key] = sys.modules.pop(key)
    yield
    for key in list(sys.modules):
        if key == "renquant_model_patchtst" or key.startswith("renquant_model_patchtst."):
            sys.modules.pop(key, None)
    for k, v in saved.items():
        sys.modules[k] = v


def _selective_importer(rmp_top_raises: bool, hf_trainer_exc: ModuleNotFoundError | None):
    """Build a __import__ replacement that scopes failures to rmp namespace.

    Args:
        rmp_top_raises: if True, importing `renquant_model_patchtst` itself
                        raises ModuleNotFoundError(name='renquant_model_patchtst').
                        If False, top-level import succeeds with a fake stub.
        hf_trainer_exc: exception to raise when importing
                        `renquant_model_patchtst.hf_trainer`. None = succeeds
                        (not used by these tests, included for completeness).
    """
    real = builtins.__import__

    def _imp(name, globals=None, locals=None, fromlist=(), level=0):
        # When CPython does `from a.b import c`, it calls
        # __import__("a.b", ..., fromlist=("c",)). So for the rmp namespace
        # we have to handle BOTH bare top-level imports AND submodule
        # imports that traverse the top.
        if name == "renquant_model_patchtst" or name.startswith("renquant_model_patchtst."):
            if rmp_top_raises:
                # Top-level missing — any rmp[.*] import fails as ModuleNotFoundError
                # with name == "renquant_model_patchtst" (Python's actual behavior
                # when the top-level package isn't on sys.path).
                raise ModuleNotFoundError(
                    f"No module named {'renquant_model_patchtst'!r}",
                    name="renquant_model_patchtst",
                )
            # rmp top-level "installed" (fake stub). hf_trainer behavior depends
            # on hf_trainer_exc.
            stub = sys.modules.get("renquant_model_patchtst")
            if stub is None:
                stub = types.ModuleType("renquant_model_patchtst")
                stub.__path__ = []
                sys.modules["renquant_model_patchtst"] = stub
            if name == "renquant_model_patchtst":
                return stub
            # name == "renquant_model_patchtst.hf_trainer" (or deeper)
            if hf_trainer_exc is not None:
                raise hf_trainer_exc
            return real(name, globals, locals, fromlist, level)
        return real(name, globals, locals, fromlist, level)

    return _imp


def _setup_data_root(tmp_path, monkeypatch):
    """Configure RENQUANT_DATA_ROOT pointing at tmp_path with sentinel present
    but with NO scripts/patchtst_hf.py, so the fallback branch raises
    a known ImportError we can match on."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sec_fundamentals_daily.parquet").write_bytes(b"\x00")
    # Reset _data_root cache so the env-var change takes effect.
    from renquant_pipeline.kernel.panel_pipeline import _data_root
    _data_root._reset_cache_for_tests()
    monkeypatch.setenv("RENQUANT_DATA_ROOT", str(tmp_path))


def _dummy_artifact(tmp_path):
    p = tmp_path / "model.pt"
    p.write_bytes(b"\x00")
    return p


# Case 1: stub installed (top-level resolves, hf_trainer missing) → fallback expected.
def test_stub_package_without_hf_trainer_triggers_fallback(
    isolated_sys_modules, monkeypatch, tmp_path,
):
    monkeypatch.setattr(builtins, "__import__", _selective_importer(
        rmp_top_raises=False,
        hf_trainer_exc=ModuleNotFoundError(
            "No module named 'renquant_model_patchtst.hf_trainer'",
            name="renquant_model_patchtst.hf_trainer",
        ),
    ))
    _setup_data_root(tmp_path, monkeypatch)

    from renquant_pipeline.kernel.panel_pipeline import hf_patchtst_scorer
    with pytest.raises(ImportError, match=r"hf_trainer\.HFPatchTSTRanker unavailable.*legacy.*missing"):
        hf_patchtst_scorer.HFPatchTSTPanelScorer.load(_dummy_artifact(tmp_path))


# Case 2: package not installed at all → fallback expected.
def test_top_level_package_missing_triggers_fallback(
    isolated_sys_modules, monkeypatch, tmp_path,
):
    monkeypatch.setattr(builtins, "__import__", _selective_importer(
        rmp_top_raises=True, hf_trainer_exc=None,
    ))
    _setup_data_root(tmp_path, monkeypatch)

    from renquant_pipeline.kernel.panel_pipeline import hf_patchtst_scorer
    with pytest.raises(ImportError, match=r"hf_trainer\.HFPatchTSTRanker unavailable.*legacy.*missing"):
        hf_patchtst_scorer.HFPatchTSTPanelScorer.load(_dummy_artifact(tmp_path))


# Case 3: hf_trainer present but transformers missing → propagate, do NOT fall back.
def test_transformers_missing_propagates_not_fallback(
    isolated_sys_modules, monkeypatch, tmp_path,
):
    monkeypatch.setattr(builtins, "__import__", _selective_importer(
        rmp_top_raises=False,
        hf_trainer_exc=ModuleNotFoundError(
            "No module named 'transformers'", name="transformers",
        ),
    ))
    _setup_data_root(tmp_path, monkeypatch)

    from renquant_pipeline.kernel.panel_pipeline import hf_patchtst_scorer
    with pytest.raises(ModuleNotFoundError) as excinfo:
        hf_patchtst_scorer.HFPatchTSTPanelScorer.load(_dummy_artifact(tmp_path))
    assert excinfo.value.name == "transformers", (
        f"Expected transformers ModuleNotFoundError to propagate, got "
        f"exc.name={excinfo.value.name!r}; fallback path was incorrectly taken."
    )
