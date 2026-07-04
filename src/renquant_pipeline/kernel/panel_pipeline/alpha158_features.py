"""Inference-time alpha158 feature computation (serve grain).

Given a ticker's recent OHLCV, return the 158 alpha158 features at the
last bar (:func:`compute_alpha158_at`), or the full causal frame for the
cache path (:func:`compute_alpha158_frame`). Train-time z-score
normalization from the scorer artifact metadata is applied downstream.

ANTI-SKEW INVARIANT (campaign B8, enforced — no longer just claimed):
this module defines NO operators of its own. Every operator is imported
from ``renquant_base_data.alpha158_ops``, the ONE shared train/serve
module that the production panel builder
(``renquant_base_data.alpha158_qlib_panel``) also imports. Train ops and
serve ops are therefore the same objects; ``tests/test_alpha158_antiskew.py``
pins the identity plus the frame==at lockstep. Known, measured train/serve
grain divergences (RANK tie-handling; fp accumulation noise) are documented
in ``renquant_base_data.alpha158_ops.KNOWN_TRAIN_SERVE_DIVERGENCES`` —
report before changing.

Byte-equivalence of this import-shim with the pre-B8 hand-mirrored
implementation was proven on real prod panel rows before the swap
(1600 rows / 40 tickers: max|delta| = 0.0 exactly, both entry points).

Reference: ``qlib/contrib/data/loader.py:Alpha158DL.get_feature_config``
(read 2026-05-06). 9 KBAR + 4 PRICE + 27 rolling families x 5 windows = 158.

Usage::

    from renquant_pipeline.kernel.panel_pipeline.alpha158_features import compute_alpha158_at

    # Given an OHLCV DataFrame indexed by date for one ticker:
    feats: dict[str, float] = compute_alpha158_at(ohlcv_df, today)
    # → {'KMID': ..., 'KLEN': ..., 'ROC5': ..., ...}
"""
from __future__ import annotations

# NOTE: import must fail LOUDLY if renquant-base-data is unavailable — a
# silent local fallback would recreate the hand-mirror disease this module
# exists to kill. renquant-base-data is a declared install dependency
# (pyproject) and part of the production PYTHONPATH set (subrepo_env.sh).
from renquant_base_data.alpha158_ops import (  # noqa: F401
    EPS,
    KNOWN_TRAIN_SERVE_DIVERGENCES,
    STD_DDOF,
    WINDOWS,
    _rolling_apply,
    alpha158_feature_names,
    compute_alpha158_at,
    compute_alpha158_frame,
    greater as _greater,
    kbar_at as _kbar,
    less as _less,
    price_features_at as _price_features,
    resi_at as _resi_at,
    rolling_at as _rolling_at,
    rsquare_at as _rsquare_at,
    slope_at as _slope_at,
)

__all__ = [
    "EPS",
    "KNOWN_TRAIN_SERVE_DIVERGENCES",
    "STD_DDOF",
    "WINDOWS",
    "alpha158_feature_names",
    "compute_alpha158_at",
    "compute_alpha158_frame",
]
