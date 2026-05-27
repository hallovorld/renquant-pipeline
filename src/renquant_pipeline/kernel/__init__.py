"""Decision-tree kernel primitives lifted from the umbrella.

Per RFC §"Backfill Plan" functional-lift (copy-not-move), these modules are
copied verbatim from `backtesting/renquant_104/kernel/` into the pipeline
package and verified import-clean here. The umbrella keeps its working copy
until cutover.

Lifted slices (pure leaves — stdlib + numpy/pandas, optionally cvxpy/scipy/
pandas_market_calendars, no internal kernel imports):

Slice 1 — sizing / exits:
* ``kelly``        — fractional-Kelly position sizing
* ``exit_types``   — exit signal value types
* ``market_gates`` — buy-side market gates
* ``vol_target``   — volatility targeting
* ``sizing``       — position sizing helpers

Slice 2 — regime / intraday / config / safety / portfolio:
* ``regime_resolver``    — regime resolution helpers
* ``regime_hmm``         — HMM regime detector
* ``intraday``           — intraday bar helpers
* ``intraday_wash``      — intraday wash-sale detection
* ``config``             — config loading primitives
* ``config_consistency`` — config drift / consistency checks
* ``net_safety``         — broker-return net-safety guards
* ``realized_pnl``       — realized P&L (FIFO/HIFO) computation
* ``portfolio``          — portfolio state helpers
* ``scoring``            — score value types / helpers

Slice 3 — QP engine math + selection / rotation / exits:
* ``portfolio_qp.qp_solver``            — cvxpy/CLARABEL Markowitz QP solver
* ``portfolio_qp.signal_combiner``      — multi-signal μ combination
* ``portfolio_qp.cvxportfolio_backend`` — cvxportfolio backend (uses qp_solver)
* ``selection``        — candidate scoring, guards, tiered selection loop
* ``rotation``         — thesis-rotation pair finding
* ``rotation_convex``  — convex rotation optimization
* ``exits``            — tax-lot accounting + exit-signal evaluation

The QP *Tasks/Jobs* (``job_qp``, ``task_joint_qp``, ``tasks``) are NOT in
this slice — they depend on the pipeline orchestration core
(``kernel.pipeline.{pipeline,atoms}``), lifted in slice 4.

Slice 4 — orchestration core (``kernel.pipeline`` subpackage; see its own
``__init__`` docstring). Unlike the leaf slices this one *reconciles* onto
``renquant_common`` instead of copying verbatim: ``pipeline`` re-exports the
canonical ``Task``/``Job``/``run_parallel``/``ParallelTimeoutError``/
``resolve_workers`` and adds only a thin ``TickerJob`` + config-deriving
``run_parallel`` wrapper; ``atoms`` are verbatim reusable Task atoms.
``InferenceContext`` already lives in ``renquant_pipeline.context`` (P1).

Slice 5 — model scoring + execution backend support (verbatim):
* ``models``           — artifact scoring (manual / classification / qlearning
  / json-tree xgboost), calibration, expected-return helpers
* ``execution`` (subpackage) — sim/LEAN execution backends: ``types``,
  ``fees``, ``slippage``, ``t2_settlement``, ``backend``, ``backend_sim``,
  ``backend_lean``. Self-contained (relative imports), no LEAN runtime import
  at module load. ``models`` traverses JSON xgboost trees — it does NOT import
  the xgboost library, so the import boundary holds.

Slice 6 — regime / indicators tier (copy-AND-rewrite, NOT verbatim):
* ``regime``      — regime detector (Hurst / CUSUM / GMM / ADX layers)
* ``indicators``  — technical indicators + SPY context builders

These use absolute ``kernel.X`` imports in the umbrella, rewritten here to
``renquant_pipeline.kernel.X``. ``regime`` ↔ ``indicators`` form a *lazy*
(function-level) mutual-import cycle, so module load is clean; the rewritten
cross-imports are exercised by ``tests/test_lift_rewrite_parity.py``.

(The data-access layer ``data`` / ``data_cache`` is NOT lifted here — it
imports the alpaca SDK for ingestion and belongs in ``renquant-base-data``
per the migration manifest, not the decision pipeline.)
"""
from __future__ import annotations

__all__: list[str] = []
