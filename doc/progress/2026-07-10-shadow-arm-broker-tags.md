# Shadow-arm broker tags for the two-arm admission experiment (D6-§2a P-1b)

**Date:** 2026-07-10
**PR:** feat/shadow-arm-broker-tags
**Spec:** renquant-orchestrator #443 —
`doc/design/2026-07-09-governor-prereg-replay-protocol.md` §2a (P-1/P-2
build items). Sibling prerequisite: renquant-execution #26 (P-1,
readonly-broker parameterization).

## What

Added the two frozen experiment broker-state tags `alpaca_shadow_a`
(arm S-0.5) and `alpaca_shadow_b` (arm S-1.0 control) to
`ALLOWED_BROKERS` in BOTH state_paths copies
(`src/renquant_pipeline/state_paths.py` and
`src/renquant_pipeline/kernel/state_paths.py`, per this repo's known
duplication pattern — the spec calls this out explicitly). No other
behavior change; the allowlist remains fail-closed (`ValueError` on any
unknown tag).

## Why

The §2a two-arm shadow admission experiment needs per-arm isolated broker
state (`live_state.alpaca_shadow_a.json` / `runs.alpaca_shadow_a.db`,
and the `_b` counterparts). The tags are NEW and distinct from the legacy
`alpaca_shadow` tag, which stays owned by the untouched `daily_104.sh`
Step-4 ops shadow. Without allowlist entries the centralised path
constructor raises by design, so this tiny PR is a hard prerequisite for
the orchestrator two-arm runner (P-2).

## Tests

New `tests/test_shadow_arm_broker_tags.py` (19 tests):

- both tags accepted by `live_state_path` / `runs_db_path` in both copies;
- allowlist parity pin — the two hand-duplicated copies must stay identical;
- distinct state + DB paths across `alpaca_shadow` / `_a` / `_b` (no
  collision with the legacy tag);
- `runs_db_path` idempotence does not cross arm boundaries
  (prefix-tag vs arm-tag suffix confusion);
- genuine sentinel collision check: state written through arm A's path is
  invisible to arm B and legacy `alpaca_shadow` reads;
- unknown/traversal tags (`alpaca_shadow_c`, hyphenated forms,
  `../alpaca_shadow_a`) still rejected — fail-closed unchanged.

Full suite: **1355 passed, 7 skipped** (baseline off main 1336 + 19 new;
zero regressions).
