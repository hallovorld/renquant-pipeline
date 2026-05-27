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
"""
from __future__ import annotations

__all__: list[str] = []
