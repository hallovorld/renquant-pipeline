# 2026-08-06 — The replay's per-name cap can be production's, and says so when it is not

STATUS:   READY FOR REVIEW. 22 new tests; full suite 2490 passed / 8 skipped /
          0 failed. Behaviour for every existing caller is byte-identical — the
          new `strategy_config` kwarg defaults to None and no caller passes it
          yet.

WHAT:     `load_replay_bars_from_sim_db(..., strategy_config=None)` threads a
          strategy config to `_build_snapshot` → `_max_position_pct_for_regime`.
          With a config the replay sizes on the production per-regime cap;
          without one it uses the same replay defaults as before but logs the
          value AND its origin on every bar.

WHY/DIR:  Measured 2026-08-06 (#271): `_MAX_POSITION_PCT_BY_REGIME` has never
          matched production.

          | regime | replay | production |
          |---|---:|---:|
          | BULL_CALM | 0.15 | 0.12 |
          | BULL_VOLATILE | 0.20 (fallback) | 0.20 — coincidence |
          | CHOPPY | 0.20 (fallback) | 0.15 |
          | BEAR | 0.20 (fallback) | **0.0** |

          `BEAR` is the worst: the config says hold nothing and the replay sized
          at 20%. Only `BULL_CALM` appears in the dict; the other three fall
          through to `_DEFAULT_MAX_POSITION_PCT`.

          The separation is a legitimate DESIGN — replay defaults are not prod
          constraints, and the module said so. The defect was that it was
          **silent**: a replay used to justify a sizing change measured a
          different book and reported nothing. Concretely, strategy-104#94
          raises BULL_CALM to 0.30 and **cannot be validated by replay** while
          the loader sizes at 0.15.

EVIDENCE:
artifact:      `kernel/portfolio_qp/wf_replay_loader.py`,
               `tests/test_wf_replay_regime_cap.py`
prod or exp:   offline replay loader only. No live sizing, admission, or order
               path touched; no production config read or written.
existing data: `renquant-strategy-104/configs/strategy_config.json`
               `[VERIFIED — 2026-08-06]`; the six production read sites cited
               below.
best-known?:   yes — the production read sites are the authority and were read
               directly rather than inferred.
scope:         `_max_position_pct_for_regime`, `_build_snapshot`,
               `load_replay_bars_from_sim_db`.

## A correction the tests forced, and the more important finding

The first cut of this fix routed through `kernel/regime_resolver.py`
`resolve_regime_knob`, on my earlier claim that production resolves
`regime_params.<regime>.max_position_pct` **>** `position_sizing.max_position_pct`.
A test I wrote for that behaviour failed, and the reason is that **the claim was
wrong**:

```
grep -rn "max_position_pct" src/renquant_pipeline/kernel/pipeline/ …
  task_selection.py:159    regime_params.get("max_position_pct", 0.15)
  task_selection.py:233    regime_p.get("max_position_pct", 0.15)
  task_selection.py:763    regime_p.get("max_position_pct", 0.15)
  task_rotation.py:791     regime_p.get("max_position_pct", 0.15)
  task_joint_actions.py:289 regime_params.get("max_position_pct", 0.15)
  governor_sizing.py:165   regime_p.get("max_position_pct", 0.15)
```
`[VERIFIED — 2026-08-06]`

**No production site calls `resolve_regime_knob` for this knob, and none reads
`position_sizing.max_position_pct` at all.** That key is dead — the 0.15 it
holds is never consulted, and the 0.15 that *does* apply is a literal repeated
at six call sites, equal to it by coincidence.

Had the resolver version shipped, replay would have honoured a section
production ignores — a NEW divergence, introduced by the fix for the old one,
and invisible for the same reason. The implementation now mirrors the measured
contract: `regime_params[<regime>]["max_position_pct"]`, else production's
literal 0.15 when the regime exists without a cap.

`0.0` is handled explicitly, because any truthiness test on the resolved value
turns BEAR back into the 0.20 fallback — the exact defect.

The module docstring also claimed these defaults "mirror the per-regime
conviction cap range used by prod". They never did; that sentence is corrected
and a test pins the correction.

## NOT ESTABLISHED

1. **That any published replay conclusion changes.** Nothing was re-run. Every
   existing caller still passes no config and gets identical numbers, so no
   prior result is silently restated — but none is re-validated either.
2. **That six hardcoded 0.15 fallbacks are correct.** A regime added to the
   config without a `max_position_pct` silently takes 0.15 at six independent
   sites. Mirrored here deliberately rather than "improved", because replay must
   match production, not be better than it. Worth its own issue.
3. **Whether callers should be REQUIRED to pass a config.** Left optional so
   this change is behaviour-preserving; making it mandatory is a separate
   decision with its own blast radius.

NEXT:     Point the WF/QP replay entry points at the pinned `strategy_config.json`
          so `0.30` (strategy-104#94) becomes replay-verifiable. That is a caller
          change in `renquant-backtesting`/`renquant-orchestrator`, not here.

## REVERT

Drop the `strategy_config` parameter from the three functions, restore
`_max_position_pct_for_regime(regime)` to its two-line body, delete
`_PROD_MAX_POSITION_PCT_FALLBACK` and the logging, restore the docstring
sentence, and delete `tests/test_wf_replay_regime_cap.py`.
