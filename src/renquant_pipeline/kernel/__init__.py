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

Kernel-ownership contract (G3 F-8)
----------------------------------
This package IS the pinned, versioned contract that ``renquant-orchestrator``
(and any other consumer bootstrapping against a pinned pipeline checkout)
relies on: every entry that physically lives in this directory is expected
to import cleanly as ``renquant_pipeline.kernel.<stem>``. A consumer that
finds an entry here and fails to import it must treat that as a real,
fail-closed error — never a silent excuse to substitute another copy (e.g.
the umbrella's local ``kernel.<stem>``).

``NON_OWNED_KERNEL_STEMS`` below is the ONLY declared exception to that
default. It is deliberately tiny and reviewed: each entry needs a concrete,
documented reason a failed/absent import for that specific stem is fine.
It is NOT a general "known flaky" escape hatch, and it must never be used
to paper over an accidental umbrella-only module landing in this directory
by name coincidence — the contract is keyed off this frozenset, a
structural declaration owned by *this* package, never off the failing
stem's name matching something a consumer happens to expect.

``OWNED_KERNEL_STEMS`` is the companion, positive declaration: every stem
this package guarantees to ship in this directory (i.e. every stem NOT in
``NON_OWNED_KERNEL_STEMS``). A consumer walking a pinned checkout of this
package uses it as a path-identity / sanity check on the directory it
discovered — e.g. renquant-orchestrator's ``live_bridge.bootstrap_multirepo``
verifies the stems it actually found cover everything declared here, to
catch a wrong or incomplete pipeline checkout (PR #514 round 4; this
replaced an earlier arbitrary orchestrator-local minimum-module-count
heuristic Codex flagged as stale-prone and not tied to the real pinned
contract).
"""
from __future__ import annotations

__all__: list[str] = ["NON_OWNED_KERNEL_STEMS", "OWNED_KERNEL_STEMS"]

# Stems that physically exist in this directory but are NOT part of the
# guaranteed, importable contract above. A consumer bootstrapping against
# the pinned pipeline (see renquant-orchestrator's
# ``live_bridge.bootstrap_multirepo``) may tolerate an import failure for
# exactly these stems and fall through to another source; every other stem
# in this directory failing to import is a hard, unconditional error.
#
# - ``meta_label``: physically present here (functional-lift copy, see
#   Slice history above) but never authoritative. Consumers force-alias
#   ``kernel.meta_label`` to ``renquant_backtesting.meta_label``
#   unconditionally after probing this directory, so whether this
#   package's own copy imports cleanly has no bearing on what actually
#   serves requests under that name. See renquant-orchestrator
#   ``live_bridge.bootstrap_multirepo`` / PR #514.
NON_OWNED_KERNEL_STEMS: frozenset[str] = frozenset({
    "meta_label",
})

# The companion, positive side of the same contract: every stem that
# physically exists in this directory and IS part of the guaranteed,
# importable contract above (i.e. every stem NOT in NON_OWNED_KERNEL_STEMS).
# Maintained the same way as NON_OWNED_KERNEL_STEMS -- an explicit, reviewed
# frozenset literal, not something computed at import time by re-listing
# this same directory. A consumer that walks a PINNED CHECKOUT of this
# package (e.g. renquant-orchestrator's ``live_bridge.bootstrap_multirepo``)
# uses this declaration as the actual pinned-package contract to verify its
# discovered directory against -- if OWNED_KERNEL_STEMS is not a real,
# static "here is what should be here" declaration, but instead just infers
# itself from whatever the directory happens to contain, an empty or wrong
# directory would trivially "satisfy" it too, defeating the point (this is
# exactly the gap Codex flagged with the old orchestrator-local
# ``_MIN_PIPELINE_KERNEL_MODULES = 10`` heuristic: "bind the discovered
# module inventory to the pinned package contract instead" -- a "contract"
# that just re-derives itself from disk isn't a contract).
#
# ``tests/test_kernel_ownership_contract.py`` enforces that this set, union
# NON_OWNED_KERNEL_STEMS, always equals exactly what is physically present
# in this directory -- so an added/removed/renamed kernel module without a
# matching update to one of these two declarations fails CI here, in
# pipeline, rather than surfacing later as an orchestrator bootstrap error
# with no clear cause.
OWNED_KERNEL_STEMS: frozenset[str] = frozenset({
    "alert_lifecycle",
    "diagnostic_only_override",
    "artifact_resolver",
    "asset_class",
    "broker_reconciliation",
    "config",
    "config_schema",
    "data",
    "data_cache",
    "data_coverage",
    "decision_trace",
    "deployment_allocator",
    "deployment_governor",
    "execution",
    "exit_types",
    "exits",
    "gate_registry",
    "indicators",
    "intraday",
    "intraday_wash",
    "kelly",
    "live_state_v2",
    "market_gates",
    "model_protection",
    "models",
    "net_safety",
    "panel_pipeline",
    "persistence",
    "pipeline",
    "pit_reader",
    "portfolio",
    "portfolio_qp",
    "preflight",
    "preflight_pipeline",
    "realized_pnl",
    "regime",
    "regime_hmm",
    "regime_resolver",
    "rotation",
    "rotation_convex",
    "score_audit",
    "score_drift",
    "scoring",
    "selection",
    "sizing",
    "state_paths",
    "trade_events",
    "typed_past",
    "vol_target",
    "walk_forward",
})
