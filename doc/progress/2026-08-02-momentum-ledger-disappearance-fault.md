# Certified-then-missing momentum ledger is a load FAULT, not the designed skip (#254)

STATUS: complete on this branch — a one-branch semantic fix + 2 deterministic
regressions; behavior-inert for every healthy run (the changed branch is
reachable only when a file certified moments earlier is gone at read time).
NEXT: codex review + merge; rides the normal pin cadence (no urgency — the
momentum lane itself is still inert until the slice-5 grant batch lands the
s104 `shadow_models` entry and the pin advance).

WHAT: fixes the #253 regression found in post-merge review (issue #254).
`ApplyShadowScoringTask` only calls the momentum loader AFTER certifying
`identity.resolved` over the ledger path, but
`load_momentum_residual_scorer` mapped a subsequent `ledger.read_bytes()`
FileNotFoundError to `ShadowNotYetPublished` — recording the designed
pre-first-publish EXPECTED skip for a file that in fact DISAPPEARED between
certification and use.

- `src/renquant_pipeline/kernel/panel_pipeline/momentum_residual_scorer.py`:
  the FileNotFoundError branch of the single read now raises
  `ValueError("ledger_unreadable: … disappeared between identity
  certification and the loader's read …")` — the existing named-prefix
  fault family — instead of `ShadowNotYetPublished`. Any other read failure
  keeps the existing `ledger_unreadable:` OSError mapping.
  `ShadowNotYetPublished` is now raised ONLY for a successfully read,
  chain-verified ledger carrying zero rows (the module docstring's
  "ONE non-fault refusal" now states exactly that).
- Contract docstrings updated to match in `shadow_health.py`
  (`ShadowNotYetPublished` + the `STATE_NOT_YET_PUBLISHED` comment) and
  `model_registry.py` (`MomentumResidualHandler`). No task-side code change:
  the generic load-failure handler already stamps `STATE_LOAD_FAILED` with
  the named `load_error` and caches nothing.
- `tests/test_momentum_residual_shadow_handler.py`: two deterministic
  regressions, both following the #253 suite's fixtures (real package
  writers, real registry + resolver):
  * loader-level: publish, delete the ledger, call the loader on the
    certified path → `ValueError` matching `^ledger_unreadable:` (a plain
    `ShadowNotYetPublished` would fail the type assertion);
  * task-level (codex's resolver-to-loader deletion): the racing-resolve
    idiom deletes the ledger AFTER `resolve_artifact_identity` certifies
    it → record asserts `STATE_LOAD_FAILED` + `STATUS_FAULT` +
    `loaded is False` + `load_error` starting `ledger_unreadable:` and
    containing "disappeared" + `state != STATE_NOT_YET_PUBLISHED` + NO
    `momentum_residual` entry in `_SCORER_CACHE`.

WHY/DIR: the `not_yet_published` expected skip exists so the sentinel does
NOT alarm on the designed pre-first-publish window. Stamping it for a
vanished certified file would make the one silent-death state the lane's
health design explicitly refuses: an artifact deleted/moved out from under
the live config would read as "designed, waiting" forever instead of a
FAULT the sentinel surfaces. Fail-closed discipline (GOAL-1): every
non-designed refusal must carry its own name.

EVIDENCE:
  artifact:      tests/test_momentum_residual_shadow_handler.py (the 2 new
                 regressions + the 18 existing tests)
  prod or exp:   exp — merge-inert twice over: the changed branch executes
                 only when a certified ledger is missing at read time, and
                 the momentum lane itself is dispatched only once the s104
                 `shadow_models` entry lands (slice-5 grant batch)
  existing data: issue hallovorld/renquant-pipeline#254 (codex post-merge
                 review of #253); doc/progress/2026-08-02-momentum-residual-
                 shadow-handler.md (the #253 record whose "EMPTY (or
                 resolved-but-missing)" wording this corrects)
  best-known?:   yes — reuses the existing `ledger_unreadable:` named-fault
                 family and the task's existing generic load-failure path;
                 the alternative (a new state or task-side special case)
                 would widen surface for zero additional information
  scope:         "this is tests/test_momentum_residual_shadow_handler.py
                 (20 tests) + the full pipeline suite, exp path, vs
                 baseline = origin/main 60871e2"

  Measured counts: new suite **20 passed** (18 existing + 2 new)
  `[VERIFIED — pytest -q tests/test_momentum_residual_shadow_handler.py,
  this branch, 2026-08-02]`. Both new tests **fail on the pre-fix loader**
  (stash the src change, rerun: 2 failed) — valid regressions
  `[VERIFIED — same session]`. Full suite: **2 failed, 2376 passed,
  9 skipped** — the same 2 pre-existing `test_replay_d6_conventions`
  pin-platform failures reproduced UNCHANGED on clean origin/main 60871e2
  in this environment (`hac_t_stat: None != -0.053…`), zero regressions
  `[VERIFIED — pytest -q, this worktree, pre- and post-change]`.
