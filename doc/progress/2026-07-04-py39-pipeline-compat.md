# Python 3.9 compatibility fix — pipeline

**Date:** 2026-07-04
**PR:** pipeline fix/py39-compat-pipeline

## What

Fixed `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`
in two pipeline source files that used Python 3.10+ union syntax (`X | None`)
in contexts where Py3.9 evaluates annotations at runtime:

1. **`kernel/config.py`** — `Path | None` in function signatures without
   `from __future__ import annotations`. Added the `__future__` import and
   replaced with `Optional[Path]`.

2. **`kernel/live_state_v2.py`** — Pydantic v2 `BaseModel` subclasses with
   `float | None`, `str | None`, `EntrySignalV2 | None`, etc. Pydantic v2
   calls `get_type_hints()` which evaluates annotations at runtime regardless
   of `from __future__ import annotations`. Replaced all union annotations
   with `Optional[X]`.

## Impact

- **Before:** 52 collection errors (37 TypeError + 15 missing-module)
- **After:** 34 collection errors (0 TypeError; 34 = missing xgboost/cvxpy)
- **Net:** 18 test files unblocked; 168 additional tests now collectible

## Root cause

System Python is 3.9.6; `X | None` union syntax requires 3.10+.
Same root cause as orchestrator PR #326.
