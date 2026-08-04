# Momentum gains the primary-scorer surface — the operator can now see its orders

**Date:** 2026-08-03 · `renquant-pipeline` · GOAL-7 / pipeline#258

STATUS:    code + tests; UNWIRED in production (no committed profile serves
           momentum as primary — the rehearsal used a scratch profile on a
           readonly broker tag). Wiring = a reviewed strategy-104 profile,
           separate change.
WHAT:      Three pieces, found by running the real funnel and fixing each
           refusal in turn (four rehearsal rounds, all readonly):
           1. `MomentumResidualScorer` implements the PanelScorer serving
              contract: `feature_cols=[]`, `seq_len=1`, `score(X)` = lookup
              by the matrix INDEX; unscored names come back NaN (the primary
              path's unscored marker) rather than omitted (the shadow path's
              coverage convention).
           2. Kind-aware config consistency in `LoadScorerTask`: for
              `kind=momentum_residual` the check compares the artifact's own
              `momentum-<ver>-<16hex>` stamp against a profile-pinned
              `expected_config_fingerprint` — exact match, fail-closed on an
              absent or mismatched pin. The generic XGB-recipe comparison is
              meaningless for a lookup artifact (measured: every compared
              field stored=None on a healthy lane).
           3. `matrix_usable(scorer, X)` — ONE shared predicate for the
              assemble task + ApplyScoresTask: a matrix-less scorer needs
              only ROWS; pandas `.empty` is True on a 0-column frame with 88
              rows and fail-closed a healthy lane. Feature scorers still
              require columns.
WHY:       Operator 2026-08-03: "我要看所有shadow，特别是动量模型的下单". The
           momentum lane is an in-process ranking shadow with no order
           funnel; running it as primary crashed on the missing surface
           (pipeline#258 has the original traceback).

EVIDENCE (rehearsal rounds, readonly `alpaca_shadow` tag, real broker reads):

```
r1 (pre-fix):  AttributeError feature_cols at job_panel_scoring.py:1088
r2 (+surface): panel_scorer_config_mismatch — XGB-recipe fields vs momentum
               stamp, all stored=None
r3 (+kind-aware consistency, pinned momentum-v0-fd65161a20b29314):
               loaded 144 finite scores; X.shape=(88,0) → "empty matrix" →
               panel_score_matrix_missing
r4 (+matrix_usable): panel scored 84/84 candidates 4/4 holdings; verdict
               ECONOMIC_TRADE; **BUY WELL x3 @ $233.10** ($699, 6.4% target,
               legacy sizing); ROST/JNJ/FDX blocked by
               nonpositive_expected_return (alpha_to_mu); ntfy
               [READONLY] SHADOW-ACTION delivered.  [VERIFIED — all four
               logs in the session scratchpad momentum_rehearsal_*.log]
```

WELL sits in the momentum lane's own top3 (XLK/ASML/WELL, today's in-process
shadow) — the order surface is coherent with the ranking surface.

Tests: 34 in test_momentum_residual_shadow_handler.py (12 new: surface,
NaN contract, consistency branch incl. no-pin fail-closed, matrix_usable);
full suite 2399 passed / 0 failed.

## Twin-pairs bookkeeping (GOAL-3 mechanism, exercised as designed)

Both changed exports are kernel-only movements; the public twins have no
scorer loading / no inference-frame assembly to receive them. Two entries
added to `twin_repin_exceptions.json` (LoadScorerTask, ApplyScoresTask); the
pipeline#250 ApplyScoresTask entry is SUPERSEDED by this movement and removed
per the ledger's own rule, chained in the new entry's reason; the ledger pin
test updated in the same diff.

## Deliberately NOT done

- No committed momentum-primary profile (strategy-104 territory; needs its
  own review with the delta-6 nulls + the momentum fingerprint pin).
- No new broker tag (`alpaca_shadow` was reused for the rehearsal — freed by
  the Step-4 retirement; a durable lane should claim its own tag in
  ALLOWED_BROKERS).

## Revert

git revert. The momentum lane returns to shadow-only serving; nothing else
depends on the new surface (UNWIRED).
