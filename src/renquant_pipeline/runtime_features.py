"""Runtime feature-row alignment and normalization.

Training artifacts define the feature space. Runtime inputs must either arrive
already in that space (``feature_frame``) or declare raw/panel source space so
the same artifact metadata can transform them before scoring.
"""
from __future__ import annotations

import math
from typing import Any


RAW_FEATURE_KEYS = ("raw_feature_frame", "raw_features")
PRETRANSFORMED_FEATURE_KEYS = ("feature_frame", "features")


def build_runtime_feature_frame(
    market_snapshot: dict[str, Any],
    artifact: dict[str, Any],
    feature_cols: list[str],
    *,
    panel_config: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Return per-ticker rows aligned to artifact ``feature_cols``.

    ``feature_frame`` / ``features`` are assumed to be already in scorer
    feature space unless a source-space override is present. ``raw_features``
    and ``raw_feature_frame`` always require artifact normalization metadata.
    """
    market = market_snapshot or {}
    panel_cfg = panel_config or {}

    raw_rows = _first_mapping(market, RAW_FEATURE_KEYS)
    if raw_rows is not None:
        return transform_feature_rows(
            raw_rows,
            feature_cols,
            artifact,
            source_space=str(market.get("feature_source_space") or "raw"),
            clip=_feature_clip(market, panel_cfg, artifact),
            require_metadata=True,
        )

    rows = _first_mapping(market, PRETRANSFORMED_FEATURE_KEYS)
    if rows is None:
        return {}

    source_space = (
        market.get("feature_source_space")
        or panel_cfg.get("feature_source_space")
        or artifact.get("feature_source_space")
        or "training"
    )
    if str(source_space) in {"training", "scorer", "pretransformed", "none"}:
        return _coerce_rows(rows)
    return transform_feature_rows(
        rows,
        feature_cols,
        artifact,
        source_space=str(source_space),
        clip=_feature_clip(market, panel_cfg, artifact),
        require_metadata=True,
    )


def transform_feature_rows(
    rows: dict[str, dict[str, Any]],
    feature_cols: list[str],
    metadata: dict[str, Any],
    *,
    source_space: str,
    clip: float | None = 5.0,
    require_metadata: bool = True,
) -> dict[str, dict[str, float]]:
    """Transform raw/panel feature rows into the scorer training space."""
    feature_cols = [str(col) for col in feature_cols]
    if not feature_cols:
        return {str(ticker): {} for ticker in rows}

    means, stds, kinds, raw_low, raw_high = _stats_from_metadata(
        metadata or {},
        len(feature_cols),
        require_metadata=require_metadata,
    )
    source = source_space.strip().lower()
    if source == "raw":
        mask = [True] * len(feature_cols)
    elif source == "panel":
        mask = [kind in {"robust_z", "panel_raw_z"} for kind in kinds]
    else:
        raise ValueError(f"unknown feature source_space: {source_space}")

    transformed: dict[str, dict[str, float]] = {}
    for ticker, raw_row in rows.items():
        if not isinstance(raw_row, dict):
            raise ValueError(f"feature row for {ticker} must be a mapping")
        missing = [col for col in feature_cols if col not in raw_row]
        if missing:
            raise ValueError(f"feature row for {ticker} missing columns: {missing[:10]}")
        out: dict[str, float] = {}
        for idx, col in enumerate(feature_cols):
            value = _finite_float(raw_row[col], label=f"{ticker}.{col}")
            if source == "raw" and _has_clip(raw_low[idx], raw_high[idx]):
                value = min(max(value, raw_low[idx]), raw_high[idx])
            if mask[idx]:
                value = (value - means[idx]) / stds[idx]
            if clip is not None and clip > 0:
                value = min(max(value, -float(clip)), float(clip))
            out[col] = float(value)
        transformed[str(ticker)] = out
    return transformed


def _stats_from_metadata(
    metadata: dict[str, Any],
    n: int,
    *,
    require_metadata: bool,
) -> tuple[list[float], list[float], list[str], list[float | None], list[float | None]]:
    means_raw = metadata.get("feature_means")
    stds_raw = metadata.get("feature_stds")
    kinds_raw = metadata.get("feature_norm_kind") or metadata.get("feature_norm_kinds")
    if not _list_len(means_raw, n) or not _list_len(stds_raw, n):
        if require_metadata:
            raise ValueError("feature normalization metadata missing or length-mismatched")
        return (
            [0.0] * n,
            [1.0] * n,
            ["identity"] * n,
            [None] * n,
            [None] * n,
        )
    means = [_finite_float(v, label=f"feature_means[{i}]") for i, v in enumerate(means_raw)]
    stds: list[float] = []
    for i, value in enumerate(stds_raw):
        std = _finite_float(value, label=f"feature_stds[{i}]")
        stds.append(std if abs(std) > 1e-12 else 1.0)
    kinds = (
        [str(kind) for kind in kinds_raw]
        if _list_len(kinds_raw, n)
        else ["legacy_full_z"] * n
    )
    raw_low = _optional_float_list(metadata.get("feature_raw_clip_low"), n)
    raw_high = _optional_float_list(metadata.get("feature_raw_clip_high"), n)
    return means, stds, kinds, raw_low, raw_high


def _first_mapping(
    source: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, dict[str, Any]] | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            out: dict[str, dict[str, Any]] = {}
            for row in value:
                if not isinstance(row, dict):
                    continue
                ticker = row.get("ticker") or row.get("symbol")
                if ticker:
                    out[str(ticker)] = dict(row)
            return out
    return None


def _coerce_rows(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, float]]:
    return {str(ticker): dict(row) for ticker, row in rows.items() if isinstance(row, dict)}


def _feature_clip(
    market: dict[str, Any],
    panel_cfg: dict[str, Any],
    artifact: dict[str, Any],
) -> float | None:
    value = (
        market.get("feature_clip")
        if "feature_clip" in market
        else panel_cfg.get("feature_clip", artifact.get("feature_clip", 5.0))
    )
    if value is None:
        return None
    return _finite_float(value, label="feature_clip")


def _optional_float_list(values: Any, n: int) -> list[float | None]:
    if not _list_len(values, n):
        return [None] * n
    out: list[float | None] = []
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float("nan")
        out.append(parsed if math.isfinite(parsed) else None)
    return out


def _list_len(value: Any, n: int) -> bool:
    return isinstance(value, list) and len(value) == n


def _has_clip(low: float | None, high: float | None) -> bool:
    return low is not None and high is not None and high > low


def _finite_float(value: Any, *, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric: {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{label} must be finite: {value!r}")
    return out
