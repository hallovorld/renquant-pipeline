# Lazy panel_pipeline package init — fingerprint_dispatch no longer drags in xgboost

Date: 2026-07-04. Fixes a real import-surface regression flagged on
`renquant-backtesting#64`'s review: importing
`renquant_pipeline.kernel.panel_pipeline.fingerprint_dispatch` (added by campaign
B1's WF-loader unification) unconditionally pulled `xgboost` into backtesting's
test collection, because Python initializes a package's `__init__.py` before any
of its submodules — and `panel_pipeline/__init__.py` eagerly imported
`panel_scorer` (and `feature_matrix`/`job_panel_scoring`, both of which
transitively import `panel_scorer` too). `fingerprint_dispatch.py` itself has no
such dependency (only `renquant_common.model_fingerprint` + stdlib).

## What changed

`panel_pipeline/__init__.py`'s four eager `from .X import (...)` blocks became a
PEP 562 `__getattr__`/`__dir__` lazy-attribute table. `renquant_pipeline.kernel.panel_pipeline.PanelScorer`
etc. still work exactly as documented in the package's own "Entry points"
docstring — only the *type* of import changed (name access vs. eager module
load at package-import time), so no existing consumer's import path or
behavior changes.

## Evidence

- `import renquant_pipeline.kernel.panel_pipeline.fingerprint_dispatch` — confirmed
  `xgboost` and `panel_scorer` are NOT in `sys.modules` afterward; accessing
  `panel_pipeline.PanelScorer` still lazily loads them on demand.
- `renquant_backtesting.walk_forward.loader` (the actual regression site) imports
  cleanly with `xgboost` import blocked outright (`builtins.__import__` patched to
  raise `ModuleNotFoundError` for xgboost) — this is the exact failure class CI hit.
- Full pipeline suite: 1303 passed, 7 skipped (against base-data#35/artifacts main,
  since local base-data/artifacts checkouts were on unrelated branches).
- backtesting's loader + wf_gate tests: 197 passed, with xgboost blocked the same way.

## Why here, not in renquant-backtesting

`fingerprint_dispatch` is correctly owned by renquant-pipeline (M6 stage-2
canonicalization). Backtesting's import statement was already correct; the bug
was this package's `__init__.py` being unconditionally heavy regardless of what
a caller actually needed.
