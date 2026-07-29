# Progress: the #219 buy-floor unit guard reaches the OTHER VetoWeakBuysTask

STATUS:   delivered.

WHAT:     `renquant_pipeline/panel_scoring.py` — the second `VetoWeakBuysTask`
          implementation — now carries the same unit guard the kernel twin got
          in #219, plus the two producer-side stamps that make it fire:
          `ApplyScoresTask` records `_rank_score_domain = RAW` when a MODEL
          scorer produced the number, `ApplyGlobalCalibrationTask` records
          `PROBABILITY` on the branch that actually calibrates, and
          `VetoWeakBuysTask` refuses a RAW-vs-probability-floor comparison via
          `_block_all(ctx, "rank_score_domain_uncalibrated")`.
          New `tests/test_panel_scoring_twin_domain_lockstep.py` (8 tests) turns
          the twin's "kept in LOCKSTEP" docstring promise into a failing test
          when the two drift.

WHY/DIR:  Found while checking whether the umbrella pipeline pin could advance
          past #219/#220. #219's guard landed in the kernel implementation only,
          and the top-level public export resolves to the other one — so
          `from renquant_pipeline import VetoWeakBuysTask` handed callers the
          copy WITHOUT the safety guard. This is the same both-copies defect
          class as the disposed-lot tax-netting bug: a fix applied to one of two
          twins reads as done and is not.

          The twin's exposure is identical in kind, not merely analogous. Its
          `ApplyGlobalCalibrationTask` returns early on two branches — no
          calibration block configured, and `method in (None, "identity")` —
          leaving `panel_scores` in the scorer's own units, which
          `VetoWeakBuysTask` then compares against a probability-domain buy
          floor. On an all-negative raw scale that vetoes the entire
          cross-section and the run reports "no trade" as though the model had
          declined, when it was never actually asked.

SCOPE:    Explicit snapshot scores and declared linear weights are deliberately
          left UNSTAMPED. Those are caller-supplied and the caller owns their
          domain (`FrozenScoreScoringJob` is a legitimate replay path). Absent
          domain keeps the previous behaviour, matching the kernel's own rule.
          The RAW stamp therefore fires exactly where the kernel's does: when a
          model scorer produced the number.

          The domain constants are DUPLICATED in the twin rather than imported,
          to avoid pulling the whole kernel scoring module into the lightweight
          one. That duplication is not self-evidently safe — which is the entire
          point of the lockstep test, since duplication without a drift test is
          what produced this bug.

EVIDENCE (§4(b)):
  artifact:       `renquant-pipeline` @ `origin/main` d55bd39 + this branch;
                  `src/renquant_pipeline/panel_scoring.py`,
                  `src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py`
  prod or exp:    PROD code path (library). No production data, config, or live
                  artifact was written. Behaviour-changing only on the branch
                  that was already broken.
  existing data:  Yes — re-measured this session, not recalled:
                  - `renquant_pipeline.VetoWeakBuysTask.__module__` =
                    `renquant_pipeline.panel_scoring`; kernel twin is a
                    DIFFERENT object; `_rank_score_domain` present in the kernel
                    source and ABSENT from the public export's source.
                  - Live-lane blast radius: 72 configs under the umbrella carry
                    `ranking.panel_scoring.global_calibration`; **all 72 are
                    `true`**, including live `strategy_config.json`,
                    `strategy_config.golden.json` and `strategy_config.shadow.json`.
                    So no existing lane trips the new refusal today.
  best-known?:    Yes for the defect's existence (direct source + import
                  measurement). The reachability claim is bounded: no importer
                  of this module's `PanelScoringJob` was found in
                  `renquant-orchestrator/ops` or the umbrella `scripts/`, so the
                  twin is not believed to be on the live daily path — it IS the
                  public top-level export, which is why it is fixed rather than
                  left.
  scope:         `renquant-pipeline` only. No pin advanced, no umbrella change,
                 no live surface touched.

VERIFICATION (behaviour invariance, both runs this session):
  baseline `origin/main` d55bd39 : 51 failed, 2055 passed, 9 skipped
  this branch                    : 51 failed, 2064 passed, 8 skipped
  failure sets byte-identical (`comm` both directions empty) — 0 introduced,
  0 fixed. +9 passed = 8 new tests + 1 environmental skip->pass
  (`test_data_root_resolver.py:70` skips inside a sibling checkout and runs in a
  standalone worktree); unrelated to this change, confirmed by reading the skip
  reason rather than assuming.
  The 51 pre-existing failures are bare-checkout environment failures present
  identically on untouched `origin/main`; this PR neither causes nor fixes them.

NEXT:     The pin-advance review this was found during is NOT unblocked by it —
          advancing the umbrella `renquant-pipeline` pin past d70bd35 remains a
          separate reviewed change, and the machine-side sync remains an
          operator-authorised landing.
