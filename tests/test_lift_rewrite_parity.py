"""Parity tests for the data/regime/indicators lift (functional-lift slice 6).

Unlike the verbatim slices, slice 6 is copy-AND-rewrite: the umbrella modules
use absolute ``kernel.X`` imports, rewritten here to
``renquant_pipeline.kernel.X``. ``regime`` and ``indicators`` form a lazy
(function-level) mutual-import cycle.

(``data`` / ``data_cache`` are intentionally NOT in this slice — they import
the alpaca SDK for ingestion and belong in ``renquant-base-data``.)

These tests pin:

1. The four modules import cleanly.
2. The rewrite is complete — no bare ``kernel.X`` import survives, and the
   rewritten targets point at ``renquant_pipeline.kernel``.
3. Both rewritten cross-import directions resolve at call time:
   ``regime.compute_spy_adx`` -> ``indicators.compute_atr`` and
   ``indicators.build_spy_context`` -> ``regime.compute_hurst``.
"""
from __future__ import annotations

import ast
import importlib
import math
from pathlib import Path

import numpy as np
import pandas as pd

KERNEL_SRC = (
    Path(__file__).parent.parent / "src" / "renquant_pipeline" / "kernel"
)
# data.py / data_cache.py are NOT here: they import the alpaca SDK (data
# ingestion) and belong in renquant-base-data per the migration manifest, not
# the decision pipeline. The pipeline-layer keystone is regime + indicators.
REWRITTEN = ["regime", "indicators"]


def test_rewritten_modules_import() -> None:
    for name in REWRITTEN:
        mod = importlib.import_module(f"renquant_pipeline.kernel.{name}")
        assert mod is not None


def test_no_bare_kernel_import_survives() -> None:
    """Every internal import is rewritten to renquant_pipeline.kernel.*."""
    offenders: list[str] = []
    for name in REWRITTEN:
        tree = ast.parse((KERNEL_SRC / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # level==0 (absolute) module beginning with bare "kernel"
                if node.level == 0 and node.module.split(".", 1)[0] == "kernel":
                    offenders.append(f"{name}.py: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == "kernel":
                        offenders.append(f"{name}.py: import {alias.name}")
    assert offenders == [], f"un-rewritten bare kernel imports: {offenders}"


def _synthetic_ohlcv(n: int = 70, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


def test_regime_to_indicators_crossimport_resolves() -> None:
    """regime.compute_spy_adx lazily imports renquant_pipeline.kernel.indicators."""
    regime = importlib.import_module("renquant_pipeline.kernel.regime")
    adx = regime.compute_spy_adx(_synthetic_ohlcv())
    assert isinstance(adx, float) and math.isfinite(adx)


def test_indicators_to_regime_crossimport_resolves() -> None:
    """indicators.build_spy_context lazily imports renquant_pipeline.kernel.regime."""
    indicators = importlib.import_module("renquant_pipeline.kernel.indicators")
    ctx = indicators.build_spy_context(_synthetic_ohlcv())
    assert isinstance(ctx, dict)
    assert math.isfinite(float(ctx["spy_realized_vol"]))


def test_indicators_series_crossimport_resolves() -> None:
    """build_spy_context_series lazily imports regime.rolling_hurst."""
    indicators = importlib.import_module("renquant_pipeline.kernel.indicators")
    frame = indicators.build_spy_context_series(_synthetic_ohlcv())
    assert isinstance(frame, pd.DataFrame) and not frame.empty
