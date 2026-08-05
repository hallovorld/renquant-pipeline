"""Persist the served feature matrix + decision surface (orch#703).

Runs LAST in ``PanelScoringJob`` so what it records is the surface that
actually decided the run: features as served, plus ``rank_score`` after
calibration, ``mu``/``sigma`` after NGBoost, and the Kelly target after sizing.
Placed at the end rather than beside ``ApplyScoresTask`` for exactly that
reason — the raw scorer output alone does not explain a buy.

FAIL-OPEN BY CONSTRUCTION. This is a logging path; nothing it does may change
or stop a run. Every exception is caught here and logged, and this task always
returns ``None`` (continue).
"""
from __future__ import annotations

import logging

from renquant_pipeline.kernel.pipeline.pipeline import Task

log = logging.getLogger(__name__)


class PersistServedMatrixTask(Task):
    """Write ``<strategy_dir>/logs/served_matrix/<date>/<lane>__<run_id>.*``."""

    def run(self, ctx) -> bool | None:  # noqa: ANN001 - InferenceContext
        from renquant_pipeline.kernel.panel_pipeline.served_matrix_sink import (  # noqa: PLC0415
            build_records,
            served_matrix_dir,
            served_matrix_sink_defined,
            write_served_matrix,
        )

        config = getattr(ctx, "config", None) or {}
        if not served_matrix_sink_defined(config):
            # No strategy dir and no override: a sim or unit test. Skipping is
            # correct — scattering parquet into a bare cwd is not.
            return None
        try:
            rows, manifest = build_records(ctx)
            path = write_served_matrix(served_matrix_dir(config), rows, manifest)
        except Exception as exc:  # noqa: BLE001 - a logging path never breaks a run
            log.warning("PersistServedMatrixTask: not persisted (%s); run continues", exc)
            return None
        log.info(
            "PersistServedMatrixTask: wrote %d rows x %d features -> %s",
            manifest["n_rows"], manifest["n_feature_cols"], path,
        )
        return None


__all__ = ["PersistServedMatrixTask"]
