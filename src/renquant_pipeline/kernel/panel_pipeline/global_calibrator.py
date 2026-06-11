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

    def _curve_zero_crossing(
        self, xs: np.ndarray, ys: np.ndarray, level: float
    ) -> float | None:
        """Raw score where the piecewise-linear (xs→ys) curve crosses ``level``.

        Returns the FIRST crossing scanning from the low-raw end, or ``None``
        when the curve never reaches ``level`` (e.g. an ER head that is
        entirely positive — structurally long-only, scoring finding F3). When
        a segment is flat exactly at ``level`` we return that segment's left
        edge.
        """
        if len(xs) < 2 or len(ys) < 2:
            return None
        shifted = np.asarray(ys, dtype=float) - float(level)
        for i in range(len(shifted) - 1):
            y0, y1 = shifted[i], shifted[i + 1]
            if y0 == 0.0:
                return float(xs[i])
            if y0 * y1 < 0.0:  # strict sign change brackets a root
                x0, x1 = float(xs[i]), float(xs[i + 1])
                # linear interpolation of the zero on this segment
                return x0 + (x1 - x0) * (0.0 - y0) / (y1 - y0)
        if shifted[-1] == 0.0:
            return float(xs[-1])
        return None

    @property
    def neutral_raw(self) -> float | None:
        """Raw panel score at which the calibrated expected return is 0.

        BL-2 (2026-06-10): the "stored neutral raw anchor" the decision-tree
        audit found missing. Consumers (signal-direction gate, μ-sign checks)
        must NOT assume raw=0 is the calibrator's neutral — for the live
        PatchTST calibrator the ER=0 crossing sits near raw≈−0.13, so a
        slightly-negative raw maps to a positive μ. ``None`` means the ER head
        never crosses zero (entirely positive or entirely negative surface),
        which is itself a flag worth surfacing.
        """
        cached = self.metadata.get("neutral_raw_cached")
        if cached is not None:
            try:
                return None if cached == "none" else float(cached)
            except (TypeError, ValueError):
                pass
        return self._curve_zero_crossing(self.er_x, self.er_y, 0.0)

    @property
    def prob_neutral_raw(self) -> float | None:
        """Raw panel score at which the calibrated P(outperform) is 0.5."""
        return self._curve_zero_crossing(self.prob_x, self.prob_y, 0.5)

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

        obj = cls(
            prob_x=np.asarray(payload["probability"]["x"], dtype=float),
            prob_y=prob_y,
            er_x=np.asarray(payload["expected_return"]["x"], dtype=float),
            er_y=er_y,
            metadata=payload.get("metadata", {}),
        )
        # BL-2 (2026-06-10): surface the neutral-raw anchor at load. A
        # calibrator whose ER=0 crossing is materially away from raw=0 maps a
        # bearish raw signal to a positive μ ("laundering") — the gate must
        # not silently assume 0 is neutral. Stamp it for downstream consumers
        # and telemetry; warn loudly when the offset is non-trivial.
        nr = obj.neutral_raw
        obj.metadata["neutral_raw_cached"] = "none" if nr is None else float(nr)
        obj.metadata.setdefault("prob_neutral_raw", obj.prob_neutral_raw)
        if nr is None:
            log.warning(
                "GlobalPanelCalibration.load: ER head never crosses zero at %s "
                "(entirely one-signed surface) — μ sign cannot be anchored to "
                "the raw signal; consumers should rely on the raw-sign gate.",
                path,
            )
        elif abs(nr) > 0.02:
            log.warning(
                "GlobalPanelCalibration.load: ER=0 neutral sits at raw=%+.4f "
                "(not 0) at %s — raw scores in (%.4f, %.4f) map to a μ of the "
                "OPPOSITE sign to their raw signal. Signal-direction gating "
                "must use this anchor, not a hard-coded 0.",
                nr, path, min(0.0, nr), max(0.0, nr),
            )
        return obj


__all__ = ["GlobalPanelCalibration"]
