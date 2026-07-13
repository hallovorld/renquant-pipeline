# fix(V-006): eliminate ALLOWED_BROKERS duplication

**Date**: 2026-07-13
**PR**: pipeline fix/v006-allowed-brokers-reexport

## Change

Replace the independent `ALLOWED_BROKERS` frozenset literal in
`kernel/state_paths.py` with a re-export from the top-level
`state_paths.py`. Both modules now resolve to the same Python object,
making silent drift structurally impossible (not just CI-caught).

The existing parity test (`test_allowlist_copies_stay_identical`) and
the re-export regression test (`test_kernel_reexport_sees_new_tags`)
both pass trivially since the objects are now identical by construction.

## Verification

- 1715 passed (2 pre-existing failures in `test_replay_d6_conventions`)
- All 19 `test_shadow_arm_broker_tags` tests pass
