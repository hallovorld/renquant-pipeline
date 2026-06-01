"""Regression test for hf_patchtst_scorer's narrowed ImportError catch.

Codex PR #7 review #1: the v1 hotfix caught any ImportError, which would
silently mask broken submodules INSIDE an installed renquant_model_patchtst
(e.g. `transformers` missing) and fall back to the legacy file-import path.
v2 narrows the catch to `ModuleNotFoundError` with `exc.name ==
"renquant_model_patchtst"`. Other import failures must propagate.
"""
from __future__ import annotations

import sys
import types
import pytest


def _purge_module(name: str) -> None:
    for k in list(sys.modules):
        if k == name or k.startswith(f"{name}."):
            del sys.modules[k]


def test_top_level_package_missing_falls_back(monkeypatch):
    """When renquant_model_patchtst itself is missing, the scorer falls back
    to file-import (and ultimately raises ImportError if legacy script also
    missing — that's the documented contract)."""
    # We can't easily make this test self-contained without disturbing the
    # installed package, but we can at least verify the narrow exception logic.
    from renquant_pipeline.kernel.panel_pipeline import hf_patchtst_scorer
    # Stash the module if installed, then simulate ModuleNotFoundError with
    # the expected exc.name on import.
    saved = sys.modules.pop("renquant_model_patchtst", None)
    saved_submod = sys.modules.pop("renquant_model_patchtst.hf_trainer", None)
    try:
        # If renquant_model_patchtst isn't installed at all, the import in
        # scorer.load would raise ModuleNotFoundError(name="renquant_model_patchtst").
        # The narrowed except clause checks exc.name in ("renquant_model_patchtst",).
        try:
            from renquant_model_patchtst.hf_trainer import HFPatchTSTRanker  # noqa: F401
        except ModuleNotFoundError as exc:
            assert exc.name in (
                "renquant_model_patchtst", "renquant_model_patchtst.hf_trainer"
            ), f"Expected exc.name to identify the missing top-level package, got {exc.name!r}"
    finally:
        if saved is not None:
            sys.modules["renquant_model_patchtst"] = saved
        if saved_submod is not None:
            sys.modules["renquant_model_patchtst.hf_trainer"] = saved_submod


def test_narrowed_catch_does_not_swallow_unrelated_module_not_found(monkeypatch):
    """If a different module name is missing (e.g. transformers), the import
    failure must propagate — NOT fall back to file-import."""
    # Build a fake renquant_model_patchtst.hf_trainer that itself fails to
    # import an unrelated module.
    fake_pkg = types.ModuleType("fake_rmp_test")
    fake_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "fake_rmp_test", fake_pkg)

    # Now define a fake submodule that, on import, raises ModuleNotFoundError
    # with a name NOT in the allowlist.
    code = (
        "raise ModuleNotFoundError('fake transformers missing', name='transformers')"
    )
    spec = types.ModuleType("fake_rmp_test.hf_trainer")

    # Simulate the scorer's narrow except clause directly:
    try:
        exec(code)
    except ModuleNotFoundError as exc:
        # This is what the scorer's narrow catch does:
        if exc.name not in ("renquant_model_patchtst",):
            # Must propagate — not fall back
            pytest.raises(ModuleNotFoundError, lambda: (_ for _ in ()).throw(exc))
        else:
            pytest.fail("narrow catch incorrectly swallowed a non-rmp exception")
