"""Runtime global panel calibration artifact.

This module owns the inference-time contract for global panel calibrators:
load the JSON artifact, interpolate probability / expected-return heads, and
scale expected returns to a requested horizon. Training-time fitting stays in
the model/training repos; runtime pipeline must not import umbrella training
modules to score live data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("renquant_pipeline.kernel.panel_pipeline.global_calibrator")


@dataclass
class GlobalPanelCalibration:
    """Two monotone interpolation heads: raw score -> probability and ER."""

    prob_x: np.ndarray
    prob_y: np.ndarray
    er_x: np.ndarray
    er_y: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.prob_x = np.asarray(self.prob_x, dtype=float)
        self.prob_y = np.asarray(self.prob_y, dtype=float)
        self.er_x = np.asarray(self.er_x, dtype=float)
        self.er_y = np.asarray(self.er_y, dtype=float)
        for name, arr in (("prob_x", self.prob_x), ("er_x", self.er_x)):
            if len(arr) >= 2 and not np.all(np.diff(arr) >= 0):
                first = int(np.argmax(np.diff(arr) < 0))
                raise ValueError(
                    f"GlobalPanelCalibration: {name} must be monotonically "
                    f"non-decreasing for np.interp; first violation at {first}."
                )

    def calibrate_probability(self, raw_score: float) -> float:
        """Map raw panel score to P(outperform)."""
        if len(self.prob_x) == 0 or len(self.prob_y) == 0:
            return 0.5
        return float(
            np.interp(
                raw_score,
                self.prob_x,
                self.prob_y,
                left=self.prob_y[0],
                right=self.prob_y[-1],
            )
        )

    def calibrate_probability_vec(self, raws: np.ndarray) -> np.ndarray:
        """Vectorized ``calibrate_probability``."""
        if len(self.prob_x) == 0 or len(self.prob_y) == 0:
            return np.full(np.shape(raws), 0.5, dtype=float)
        return np.interp(raws, self.prob_x, self.prob_y, left=self.prob_y[0], right=self.prob_y[-1])

    def expected_return(self, raw_score: float, *, horizon_days: int | None = None) -> float:
        """Map raw panel score to expected excess return over the horizon."""
        if len(self.er_x) == 0 or len(self.er_y) == 0:
            return 0.0
        native = float(
            np.interp(
                raw_score,
                self.er_x,
                self.er_y,
                left=self.er_y[0],
                right=self.er_y[-1],
            )
        )
        return self._scale_expected_return_to_horizon(native, horizon_days)

    def expected_return_vec(
        self,
        raws: np.ndarray,
        *,
        horizon_days: int | None = None,
    ) -> np.ndarray:
        """Vectorized ``expected_return``."""
        if len(self.er_x) == 0 or len(self.er_y) == 0:
            return np.zeros(np.shape(raws), dtype=float)
        values = np.interp(raws, self.er_x, self.er_y, left=self.er_y[0], right=self.er_y[-1])
        if horizon_days is None:
            return values
        native_days = self._native_lookahead_days()
        if native_days is None or native_days <= 0 or int(horizon_days) == native_days:
            return values
        return values * (float(horizon_days) / float(native_days))

    def _native_lookahead_days(self) -> int | None:
        for key in ("lookahead_days_used", "lookahead_days", "er_lookahead"):
            raw = self.metadata.get(key)
            try:
                days = int(raw)
            except (TypeError, ValueError):
                continue
            if days > 0:
                return days
        return None

    def _scale_expected_return_to_horizon(self, value: float, horizon_days: int | None) -> float:
        if horizon_days is None:
            return float(value)
        native_days = self._native_lookahead_days()
        if native_days is None or native_days <= 0 or int(horizon_days) == native_days:
            return float(value)
        return float(value) * (float(horizon_days) / float(native_days))

    def save(self, path: str | Path, metadata: dict[str, Any] | None = None) -> None:
        """Write a JSON artifact using the runtime schema.

        This helper is intentionally lightweight. Model/training repos own the
        fitting gates; runtime only preserves range sanity before serializing.
        """
        prob_min = float(self.prob_y.min(initial=0.0))
        prob_max = float(self.prob_y.max(initial=0.0))
        if prob_min < -1e-9 or prob_max > 1.0 + 1e-9:
            raise ValueError(f"probability.y out of [0,1] range [{prob_min:.4f}, {prob_max:.4f}]")
        er_absmax = float(np.max(np.abs(self.er_y), initial=0.0))
        if er_absmax > 0.20 + 1e-9:
            raise ValueError(f"expected_return.y max|y|={er_absmax:.4f} exceeds 0.20 runtime bound")
        payload = self.to_payload(metadata)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, default=str, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def to_payload(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        merged_meta = {**self.metadata, **(metadata or {})}
        return {
            "version": 1,
            "kind": "global_panel_calibration",
            "trained_date": str(date.today()),
            "probability": {"x": self.prob_x.tolist(), "y": self.prob_y.tolist()},
            "expected_return": {"x": self.er_x.tolist(), "y": self.er_y.tolist()},
            "metadata": merged_meta,
        }

    @classmethod
    def load(cls, path: str | Path) -> "GlobalPanelCalibration":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("kind") != "global_panel_calibration":
            raise ValueError(f"Not a global_panel_calibration artifact: {path}")

        prob_y = np.asarray(payload["probability"]["y"], dtype=float)
        er_y = np.asarray(payload["expected_return"]["y"], dtype=float)

        prob_y_min = float(prob_y.min(initial=0.0))
        prob_y_max = float(prob_y.max(initial=0.0))
        er_y_absmax = float(np.max(np.abs(er_y), initial=0.0))
        if prob_y_min < 0.0 or prob_y_max > 1.0:
            log.warning(
                "GlobalPanelCalibration.load: probability.y out of [0,1] "
                "range [%.4f, %.4f] at %s; clipping.",
                prob_y_min,
                prob_y_max,
                path,
            )
            prob_y = np.clip(prob_y, 0.0, 1.0)
        if er_y_absmax > 0.20:
            log.warning(
                "GlobalPanelCalibration.load: expected_return.y max|y|=%.4f "
                "> 0.20 at %s; clipping.",
                er_y_absmax,
                path,
            )
            er_y = np.clip(er_y, -0.20, 0.20)

        return cls(
            prob_x=np.asarray(payload["probability"]["x"], dtype=float),
            prob_y=prob_y,
            er_x=np.asarray(payload["expected_return"]["x"], dtype=float),
            er_y=er_y,
            metadata=payload.get("metadata", {}),
        )


__all__ = ["GlobalPanelCalibration"]
