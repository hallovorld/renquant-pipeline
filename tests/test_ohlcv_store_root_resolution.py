"""Regression: the default OHLCV store must follow the operator's repo root.

2026-06-09: ``_REPO_ROOT_OHLCV`` was anchored to *this module's* repo root
(``Path(__file__).parents[3]``). Running from the multirepo runtime clone
(``.subrepo_runtime/repos/renquant-pipeline/``) that resolved to the clone's
own near-empty ``data/ohlcv`` instead of the umbrella's full-history store —
SPY came back with ~1y of rows, the sim feature cache clipped to empty, and
every weekly_wf_promote sim cut reported zero trades.
"""
from __future__ import annotations

from pathlib import Path

from renquant_pipeline.kernel.data import LocalStore, _resolve_default_ohlcv_dir


def test_explicit_ohlcv_dir_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("RENQUANT_OHLCV_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("RENQUANT_REPO_ROOT", str(tmp_path / "umbrella"))
    assert _resolve_default_ohlcv_dir() == tmp_path / "store"


def test_repo_root_env_resolves_umbrella_store(monkeypatch, tmp_path):
    monkeypatch.delenv("RENQUANT_OHLCV_DIR", raising=False)
    monkeypatch.setenv("RENQUANT_REPO_ROOT", str(tmp_path))
    assert _resolve_default_ohlcv_dir() == tmp_path / "data" / "ohlcv"
    assert LocalStore().data_dir == tmp_path / "data" / "ohlcv"


def test_fallback_is_module_anchor(monkeypatch):
    monkeypatch.delenv("RENQUANT_OHLCV_DIR", raising=False)
    monkeypatch.delenv("RENQUANT_REPO_ROOT", raising=False)
    resolved = _resolve_default_ohlcv_dir()
    # repo root of THIS checkout: src/renquant_pipeline/kernel/data.py → parents[3]
    import renquant_pipeline.kernel.data as data_mod
    expected = Path(data_mod.__file__).resolve().parents[3] / "data" / "ohlcv"
    assert resolved == expected


def test_explicit_data_dir_overrides_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("RENQUANT_OHLCV_DIR", str(tmp_path / "ignored"))
    store = LocalStore(data_dir=tmp_path / "explicit")
    assert store.data_dir == tmp_path / "explicit"
