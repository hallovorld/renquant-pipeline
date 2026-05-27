"""Decision-tree kernel primitives lifted from the umbrella.

Per RFC §"Backfill Plan" functional-lift (copy-not-move), these modules are
copied verbatim from `backtesting/renquant_104/kernel/` into the pipeline
package and verified import-clean here. The umbrella keeps its working copy
until cutover.

Lifted slices (pure leaves — stdlib + numpy/pandas only, no internal
kernel imports):

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
"""
from __future__ import annotations

__all__: list[str] = []
