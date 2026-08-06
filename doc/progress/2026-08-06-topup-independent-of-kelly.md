# 2026-08-06 — Top-up is no longer gated by Kelly's enable flag (cash-drag P0)

STATUS:   READY FOR REVIEW. 17 new tests; full suite 2485 passed / 8 skipped /
          0 failed. **Turns top-up on nowhere.** No config in the tree carries a
          `ranking.top_up` section, so every lane resolves exactly as before.

WHAT:     `TopUpHeldTask` resolved its enable check and its knobs straight out
          of `ranking.kelly_sizing`. It now goes through
          `resolve_topup_enablement(config)`, which prefers
          `ranking.top_up.enabled` when that key is PRESENT and otherwise falls
          back to `ranking.kelly_sizing.enabled` — today's behaviour, byte for
          byte.

WHY/DIR:  Operator P0 2026-08-06: "解决 money drag 的问题".

          The drag is arithmetic, not a bug in any one place
          `[VERIFIED — 2026-08-06]`:

```
pinned max_position_pct                          0.12
confidence_to_size_multiplier(conf=0.57)         0.57
  -> per-position ceiling                        6.84%
x max_concurrent_positions                       8
  -> MAXIMUM deployment                          54.7%
  -> hard cash floor                             45.3%

live book: 46.6% invested / 53.4% cash, 7 of 8 slots used
           (8.1pp under the ceiling = exactly the one empty slot)
```

          No sequence of good trades can cross that floor. Two of the three
          multiplicands are already spoken for — the cap is strategy-104#94
          (0.12 -> 0.30) and the slot count is an operator decision. The third
          lever is the one nobody was using: **top-up is the only buy path that
          can raise the weight of a position the book ALREADY holds**; every
          other path needs a free slot.

          It was switched off, and not for a reason about top-up. Kelly is
          `enabled=false` since 2026-08-04, and the config says why in its own
          note: `use_calibrator_mu` wires Kelly's mu from the calibrator, and
          the z-blend promotion turned `global_calibration` off, so that INPUT
          ceased to exist. Top-up was collateral damage — its own conviction
          test reads `rank_score` (`topup_conviction_floor`), which the blend
          produces normally `[VERIFIED — task_topup.py:225-230 pre-change]`.
          Nothing top-up needs went away.

EVIDENCE:
artifact:      `kernel/pipeline/task_topup.py`, `tests/test_topup_enablement.py`
prod or exp:   **prod code path**, behaviour-preserving. The gate moves behind a
               resolver whose fallback is the existing expression; no config
               opts in, so no lane changes.
existing data: live Alpaca account and the pinned `strategy_config.json`
               2026-08-06; `logs/daily_104/2026-08-06.log` mentions top-up
               **0 times** — the task returned at its gate on every run.
best-known?:   yes for the coupling. **No** for "enabling top-up deploys the
               cash profitably" — untested, and deliberately not enabled here.
scope:         `TopUpHeldTask.run` enable check + three knob reads.

Root cause read to the line rather than inferred: `task_topup.py:129` was

```python
kelly_cfg = ctx.config.get("ranking", {}).get("kelly_sizing", {})
if not kelly_cfg.get("enabled", False):
    return
```

an unconditional early return. That was the whole gate.

The resolver keys on the PRESENCE of `top_up.enabled`, not its truthiness:
`enabled: false` is a decision, and resolving on truthiness would silently fall
back to Kelly and re-couple the two in one direction. A test pins that.

## NOT ESTABLISHED

1. **That enabling top-up is profitable.** Untested, unbacktested, no prereg.
   This PR only makes the decision *possible* to take on its own merits.
2. **That top-up alone clears the drag.** It cannot exceed the same 6.84%
   per-position ceiling; it can only move existing weights toward it. The cap
   itself is strategy-104#94.
3. **That `confidence_to_size_multiplier` = 0.57 is correct.** It halves every
   cap the operator sets and has never been separately reviewed. Untouched
   here, and it is the larger of the two remaining multiplicands.

## NEXT

Enabling is a separate config decision (`ranking.top_up.enabled: true` in
strategy-104) with its own evidence and its own review — deliberately not
bundled, because a code change that silently starts deploying live cash is the
shape this repo's contracts exist to prevent.

## REVERT

Delete `resolve_topup_enablement`, restore the three lines at the old gate
(`kelly_cfg.get("enabled", False)` and the two `kelly_cfg.get(...)` knob reads),
and delete `tests/test_topup_enablement.py`. No other file changes.
