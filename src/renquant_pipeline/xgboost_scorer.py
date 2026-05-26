"""Lazy XGBoost panel-artifact scorer.

The module is safe to import without xgboost installed. The dependency is
imported only when a real GBDT artifact must be scored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass
class XGBoostPanelScorer:
    feature_cols: list[str]
    booster: Any

    def predict_rows(self, rows: dict[str, dict[str, Any]]) -> dict[str, float]:
        if not rows:
            return {}
        xgb = _xgb()
        tickers = list(rows)
        matrix = [
            [_as_float(rows[ticker][col], ticker=ticker, col=col) for col in self.feature_cols]
            for ticker in tickers
        ]
        preds = self.booster.predict(xgb.DMatrix(matrix))
        return {ticker: float(pred) for ticker, pred in zip(tickers, preds, strict=True)}


def load_xgboost_panel_scorer(artifact: dict[str, Any]) -> XGBoostPanelScorer | None:
    """Load a scorer from an artifact dict or local artifact path.

    Returns ``None`` when the manifest intentionally does not point at a local
    XGBoost payload. Raises for broken local payloads so callers can fail
    closed with an explicit reason.
    """
    payload = _artifact_payload(artifact)
    if payload is None:
        return None
    if payload.get("kind") not in {None, "panel_ltr_xgboost"}:
        return None
    booster_raw = payload.get("booster_raw_json")
    feature_cols = payload.get("feature_cols") or payload.get("feature_columns")
    if not booster_raw:
        return None
    if not isinstance(feature_cols, list) or not feature_cols:
        raise ValueError("XGBoost panel artifact missing non-empty feature_cols")

    booster = _xgb().Booster()
    booster.load_model(bytearray(str(booster_raw).encode("utf-8")))
    return XGBoostPanelScorer(feature_cols=[str(col) for col in feature_cols], booster=booster)


def _artifact_payload(artifact: dict[str, Any]) -> dict[str, Any] | None:
    if artifact.get("booster_raw_json"):
        return artifact
    path = _local_artifact_path(artifact)
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"panel artifact file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"panel artifact file must contain a JSON object: {path}")
    return payload


def _local_artifact_path(artifact: dict[str, Any]) -> Path | None:
    for key in ("local_artifact_path", "artifact_path", "path"):
        value = artifact.get(key)
        if value:
            return Path(str(value)).expanduser()
    uri = artifact.get("uri")
    if not uri:
        return None
    parsed = urlparse(str(uri))
    if parsed.scheme == "file":
        return Path(parsed.path).expanduser()
    if parsed.scheme:
        return None
    return Path(str(uri)).expanduser()


def _xgb():
    import xgboost as xgb  # noqa: PLC0415

    return xgb


def _as_float(value: Any, *, ticker: str, col: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric feature {col!r} for {ticker}: {value!r}") from exc
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"non-finite feature {col!r} for {ticker}: {value!r}")
    return out
