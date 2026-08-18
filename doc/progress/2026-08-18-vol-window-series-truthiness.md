# vol-window license: Series-truthiness crash on the maiden session (hotfix)

STATUS:    fix — the lane's FIRST real session crashed at
           vol_window_license.py:253 (`Series or {}` → "truth value of a Series
           is ambiguous"). Exactly the failure class shadow-first exists to catch:
           the builder's tests used dict fixtures; the real ctx carries a pandas
           Series.

WHAT:      Sentinel `is None` replaces the `or {}` truthiness at the
           top_decile_by_score call site (that function already handles Series
           natively via .items()). One regression test: Series-typed
           _panel_scores_all through both top_decile_by_score and the full
           evaluate_vol_window_license path.

WHY/DIR:   Operator directive 2026-08-18 ("直接进shadow！现在就开始跑！") — the
           manual maiden session (readonly broker, lane-isolated) surfaced the
           crash immediately after the egress outage cleared. Preflights had all
           passed; the crash is in the license evaluation itself.

EVIDENCE:
  artifact:      the one-line-plus-comment fix + 1 regression test + this doc.
  prod or exp:   neither — lane-flag-gated module; prod lanes never reach this
                 code path (unreachable-without-flag tests unchanged).
  existing data: [VERIFIED] maiden-session log 2026-08-18_shadow_vol_window_manual.log:
                 ValueError at vol_window_license.py:253; P-WF-GATE and all
                 preflights green before the crash.
  best-known?:   yes — minimal sentinel fix; top_decile_by_score confirmed
                 Series-compatible (uses .items()); 46/46 module tests pass.
  scope:         "fixes the truthiness crash. No behavior change for dict inputs;
                 no other code touched. Pin advance to the runtime is the
                 already-authorized deploy batch's fast-follow."

TESTS:     tests/test_vol_window_license.py 46 passed (45 pre-existing + 1 new).

NEXT:      codex review → merge → pin bump (same authorized deploy batch) → rerun
           the maiden session.
