# vol-window lane broker tag registration (one-line fix + tests)

STATUS:    fix — registers `alpaca_shadow_vol_window` in ALLOWED_BROKERS BEFORE the
           lane's first session. Closes the gap impl PR 2's builder found.

WHAT:      `src/renquant_pipeline/state_paths.py`: add the tag (kernel copy is a
           re-export — single source, V-006). `tests/test_shadow_arm_broker_tags.py`:
           2 new tests in the house idiom (tag accepted in both copies; state/db paths
           disjoint from alpaca_shadow / _blend / _blend_mom).

WHY/DIR:   The alpaca_shadow_blend_mom incident class: that lane's tag was registered
           AFTER it shipped, and session 1's state write raised ValueError (recorded in
           the allowlist's own comments). The vol-window lane (design orch#1004; impl
           pipeline#294 + s104#99 + RQ#594 + orch#1005) would hit the same wall on its
           first scheduled session; this registers the tag ahead of any run.

EVIDENCE:
  artifact:      the one-line allowlist entry + 2 tests + this doc.
  prod or exp:   neither — an allowlist constant + tests; no live change (the lane is
                 not yet scheduled anywhere; deploys operator-gated).
  existing data: [VERIFIED] `alpaca_shadow_vol_window` absent from ALLOWED_BROKERS on
                 main; the _blend_mom precedent recorded in the file's own comment
                 ("measured missing on session 1: state write raised ValueError");
                 kernel/state_paths.py re-exports the top copy (single source).
  best-known?:   yes — registered before first use, with disjointness tests matching
                 the file's own pattern; no other config or code touched.
  scope:         "one allowlist entry + tests. Enables the vol-window lane's state
                 writes when it is eventually deployed; changes nothing until then."

TESTS:     tests/test_shadow_arm_broker_tags.py 35 passed (33 pre-existing + 2 new);
           full suite 2671 passed / 7 skipped (baseline had 2 platform-pin failures on
           unmodified main earlier today; none present in this run).

NEXT:      codex review → merge → included in the vol-window deploy bundle's pin
           advance (operator-gated).
