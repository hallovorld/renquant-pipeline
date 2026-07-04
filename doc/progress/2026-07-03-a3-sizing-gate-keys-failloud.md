# A3 — fail-loud on missing sizing/gate keys (kills the silent-default class)

Date: 2026-07-03
Campaign: design-compliance A3 (audit `doc/audit/2026-07-03-design-compliance-audit.md`
§5, PR #168; orchestrator PR #297)
Branch: `fix/kelly-sigma-horizon-failloud`

## Problem

The audit's P0 (§5.1) plus the P1 divergent-defaults cluster (§5.2-5.7): the
pipeline hardcodes runtime fallbacks that CONTRADICT the strategy-104 value
for the same key, and preflight passed on the absent key. Losing a key
(side-config / replay config / sim config / refactor) silently flips live
semantics with green checks. Worst case: `kelly_sizing.sigma_horizon_days`
absent ⇒ runtime default 252 ⇒ the documented 2026-06-11 Kelly bug (~4.2x
variance inflation, high-vol names crushed) re-arms itself while
P-KELLY-SIGMA-HORIZON reports "using default 252" as a PASS.

## Protection contract (P0)

Behavior with the key PRESENT is byte-identical. Only the missing-key path
changes: fail closed, naming the key. Proven empirically: a probe covering a
23-value present-key grid for `_kelly_sigma_horizon_days`, the σ rescale
grid, end-to-end `ApplyKellySizingTask` targets/blocked/counters (h=60 and
h=252), and every present-key preflight branch produces byte-identical
output on origin/main vs this branch (62 probe lines, `diff` clean). All
three real strategy-104 configs (active/golden/shadow) pass both gates.

## Changes

1. **P-KELLY-SIGMA-HORIZON absent-key branch** (`kernel/preflight.py`):
   absent + kelly enabled ⇒ HARD FAIL citing the 2026-06-11 incident.
   Absent + kelly disabled ⇒ pass with the exemption documented (the value
   is never consumed; ApplyKellySizingTask no-ops first). Present-key
   validation unchanged.
2. **Runtime defense in depth** (`kernel/panel_pipeline/job_panel_scoring.py`
   `_kelly_sigma_horizon_days`): the silent 252.0 default is gone; a missing
   key RAISES at scoring time with a message pointing at preflight. Only
   reachable when kelly is enabled. Present-key parsing byte-identical.
3. **New P-SIZING-GATE-KEYS preflight gate** (`_check_sizing_gate_keys` +
   `preflight_pipeline/tasks/sizing_gate_keys.py`, wired into
   `_RiskConfigJob` and `_LEGACY_CHECK_ORDER`): presence-only fail-closed
   sweep of the audit §5 divergent-defaults cluster —

   | key | silent flip on loss | armed |
   |---|---|---|
   | ranking.kelly_sizing.fractional | 0.3→0.25 | kelly on |
   | ranking.kelly_sizing.max_concentration | 0.12→0.35 (~3x looser) | kelly on |
   | ranking.kelly_sizing.topup_conviction_floor | 0.55→0.20 | kelly on |
   | model_staleness_days | 60→0 = staleness admission gate OFF | always |
   | rotation.min_expected_advantage_pct | 0.06→0.03 (bar halves) | rotation on |
   | rotation.joint_actions.qp_sigma_horizon_mode | match_mu→none | joint_actions on |
   | rotation.joint_actions.qp_sigma_unit | annualized→horizon | joint_actions on |
   | rotation.joint_actions.qp_horizon_contract | strict→warn (fails open); legacy alias `qp_mu_contract` satisfies | joint_actions on |
   | rotation.joint_actions.qp_tax_lot_method | hifo→fifo (tax economics); documented fallback `tax.lot_method` satisfies | always (trade_events sell path) |

   Disarmed-and-absent keys pass with the exemption recorded in details —
   that is the "absent-is-legitimate" documentation for keys whose consumer
   is off. Present-key VALUES are never judged (presence only), so no
   present-key behavior changes anywhere in the sweep.

## Deliberate scope choices

- The sweep keys keep their runtime defaults (preflight-only protection):
  removing those defaults touches live decision code paths for zero
  additional coverage once preflight fails closed before scoring; the P0
  sigma-horizon key alone also gets the runtime raise (defense in depth).
- The kelly-disabled exemption follows the audit's own recommended fix
  ("absent-key branch FAILS when kelly … on") — failing a config whose
  consumer is off would pressure operators to weaken the gate, and the
  runtime raise still guards the enable-without-key flip.

## Tests

- `tests/test_sizing_gate_keys_preflight.py` (new): prod-shaped pass, each
  of the 9 armed-missing keys fails naming the key, multi-missing reported,
  disarmed exemptions, alias/fallback acceptance, presence-only pin,
  run_preflight wiring, present-key byte-identical grid vs an inline copy of
  the pre-A3 implementation, and the raise path.
- `tests/test_kelly_sigma_horizon.py`: implicit-default test replaced by the
  raise-path pin (+ kelly-disabled inert pin); explicit-252 regression kept.
- `tests/test_kelly_sigma_horizon_preflight.py`: absent-key now pins FAIL
  (enabled) and documented pass (disabled).
- Fixture pins: `test_kelly_holdings_log_clarity.py` (sigma_horizon_days=252
  explicit), `test_preflight_config_schema.py` (always-armed keys added).
- Contract counts: preflight battery 21 → 22 checks.

Full suite: 1291 passed, 7 skipped. `make doctor` ok.

## Deploy note

Pipeline-side only; no config change needed (all keys already present in
active/golden/shadow). Goes live at the next pin bump. If a side/replay
config lacks any armed key, its next preflight fails closed naming the key —
that is the intended behavior, fix the config not the gate.
