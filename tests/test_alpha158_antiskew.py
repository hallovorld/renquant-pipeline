"""Anti-skew invariant for the live XGB primary feature path (campaign B8).

The serve module (``alpha158_features``) must define NO operators of its own:
every operator must BE (object identity) the shared
``renquant_base_data.alpha158_ops`` object that the production panel builder
(``renquant_base_data.alpha158_qlib_panel``) also uses. Train ops == serve
ops by construction — the invariant the old docstring claimed but nothing
enforced (audit 2026-07-03 §6.3, pipeline#168 top P1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from renquant_base_data import alpha158_ops as ops
from renquant_base_data import alpha158_qlib_panel as panel
from renquant_pipeline.kernel.panel_pipeline import alpha158_features as serve


def test_serve_ops_are_shared_ops_by_identity():
    assert serve.compute_alpha158_at is ops.compute_alpha158_at
    assert serve.compute_alpha158_frame is ops.compute_alpha158_frame
    assert serve.alpha158_feature_names is ops.alpha158_feature_names
    assert serve._kbar is ops.kbar_at
    assert serve._price_features is ops.price_features_at
    assert serve._rolling_at is ops.rolling_at
    assert serve._slope_at is ops.slope_at
    assert serve._rsquare_at is ops.rsquare_at
    assert serve._resi_at is ops.resi_at
    assert serve._greater is ops.greater
    assert serve._less is ops.less
    assert serve.WINDOWS is ops.WINDOWS
    assert serve.EPS == ops.EPS
    assert serve.STD_DDOF == ops.STD_DDOF


def test_train_ops_equal_serve_ops():
    """Close the loop: the TRAIN builder and the SERVE module share the same
    operator objects — the anti-skew invariant itself."""
    assert panel.kbar_features is ops.kbar_features
    assert panel.rolling_features is ops.rolling_features
    assert panel.price_features is ops.price_features
    # Both grains resolve to ONE module: any operator edit happens exactly
    # once, in renquant_base_data.alpha158_ops.
    assert serve._slope_at.__module__ == "renquant_base_data.alpha158_ops"
    assert panel.rolling_features.__module__ == "renquant_base_data.alpha158_ops"


def test_no_local_operator_definitions_in_serve_module():
    """The serve module must stay a pure re-export shim: any `def` reintroduces
    the hand-mirror disease."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(serve))
    defs = [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert defs == [], f"serve module defines local functions: {defs}"


def test_feature_names_stable():
    names = serve.alpha158_feature_names()
    assert len(names) == 158
    assert names[0] == "KMID" and names[9] == "OPEN0"
    assert names[13:158:29][0] == "ROC5"  # rolling block starts at ROC5


def test_frame_matches_at_lockstep():
    """compute_alpha158_frame (cache path) == compute_alpha158_at (live path)
    within fp-accumulation noise — pinned here because sim/cache speed must
    not buy a live/sim feature drift. (Replaces the phantom
    tests/test_feature_cache.py reference of the pre-B8 docstring.)"""
    rng = np.random.default_rng(19)
    n_bars = 200
    dates = pd.bdate_range("2024-06-03", periods=n_bars)
    close = 80.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, n_bars)))
    spread = np.abs(rng.normal(0.0, 0.006, n_bars)) + 1e-4
    open_ = close * (1 + rng.normal(0, 0.003, n_bars))
    ohlcv = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + spread),
            "low": np.minimum(open_, close) * (1 - spread),
            "close": close,
            "volume": rng.integers(200_000, 3_000_000, n_bars).astype(float),
        },
        index=dates,
    )
    frame = serve.compute_alpha158_frame(ohlcv)
    names = serve.alpha158_feature_names()
    for dt in ohlcv.index[-20:]:
        at = serve.compute_alpha158_at(ohlcv, dt)
        assert at and len(at) == 158
        row = frame.loc[dt]
        for name in names:
            a, b = float(row[name]), float(at[name])
            if np.isnan(a) and np.isnan(b):
                continue
            assert a == pytest.approx(b, abs=1e-8), (
                f"{name}@{dt.date()}: frame={a!r} at={b!r}")


def test_known_divergences_documented():
    assert "RANK" in serve.KNOWN_TRAIN_SERVE_DIVERGENCES
