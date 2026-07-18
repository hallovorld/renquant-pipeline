# Progress: VetoWeakBuys small-n guard — stage 1 implementation

Date: 2026-07-18

## What

Stage 1 of the approved RFC
`doc/design/2026-07-17-vetoweakbuys-smalln-guard.md` (merged as pipeline
#204; evidence renquant-orchestrator #543): the RELAX-ONLY small-n guard
in `VetoWeakBuysTask`, implemented in BOTH twins —
`kernel/panel_pipeline/job_panel_scoring.py` and `panel_scoring.py` —
plus the §3.2 test battery. Mechanism only: no config activation (the
strategy-104 config PR is a separate review), no sentinel (orchestrator
PR), no pins.

- `_smalln_guard_params` (§2.2 matrix): both optional keys
  (`buy_floor_min_n` int in [2, 30], `buy_floor_absolute_smalln` finite
  in (0, 1)) must be present AND valid; anything else — half-config in
  either direction or out-of-bounds — is REJECTED at ERROR (offending
  key + value in the message) and the status-quo floor applies.
  Both-absent is bit-identical status quo with no log. Validation is
  per-run whenever an adaptive mode is active, independent of n, so a
  misconfig is loud on normal-n days too.
- `_apply_smalln_guard` (§2.1): when finite-score n < `buy_floor_min_n`,
  `floor = max(buy_floor_min, min(F_mode, buy_floor_absolute_smalln))`
  where F_mode is the EXACT status-quo output of the mode formula;
  applied to all three adaptive modes (`adaptive_mean_std`,
  `adaptive_mean_std_cap`, `adaptive_quantile`); label
  `smalln-relax(n=… < N0, min(mode=…, abs=…)) = …` for grep-ability.
  NaN/None/finite-n semantics unchanged (guard n = finite-score count).
- Approval-note hardening: the §2.1 formula is wrapped in an
  unconditional one-sidedness clamp `min(F_mode, ·)`. Under the
  pre-existing pathological misconfig `buy_floor_adaptive_cap <
  buy_floor_min` (cap mode), F_mode = cap < min_fl and the raw formula
  would RAISE the floor to min_fl; the clamp degrades it to exactly the
  status-quo floor instead. For every non-pathological configuration the
  clamp is a no-op (algebraically equal to the RFC §2.1 formula whenever
  F_mode ≥ buy_floor_min). Test-proven.
- Twin port: `panel_scoring.py` previously lacked the adaptive modes
  entirely (a string `buy_floor` parsed to floor 0.0). The three modes +
  guard are ported to `_buy_floor_info`, mirroring the kernel formulas
  (statistics.fmean/stdev, identical quantile interpolation, identical
  fallbacks and labels); non-adaptive values keep the module's
  historical lenient parse. Lockstep is pinned by cross-twin
  bit-identity tests.
- Audit surface (additive, both twins): the exact floor + label are
  exposed as `ctx._panel_buy_floor` / `ctx._panel_buy_floor_label` so
  replay harnesses and tests assert bit-identity without parsing logs.

## Tests (RFC §3.2 (a)-(f))

New: `tests/test_vetoweakbuys_smalln_guard.py` (51 tests) +
`tests/fixtures/vetoweakbuys_20260710_recorded_scores.json` (recorded
live 2026-07-10 n=85 session, decision-ledger provenance,
rid=2026-07-10-live-6f9d5284).

- (a) n≥N0 bit-identity: the unguarded formula reproduces the RECORDED
  live 07-10 floor bit-for-bit (0.5441332563916457); guarded runs are
  `==`-identical (floor, label, kept set) in all three modes; boundary
  n == N0 is status quo (strict `<`).
- (b) recorded 07-16/07-17 n=5 sessions (hardcoded full-precision
  fixtures; 3dp ATI .557 EME .548 BWXT .533 / BWXT .564 EME .559
  ATI .558, XLI .449 XLY .448): status quo reproduces the recorded
  all-veto floors (0.561104…/0.576500…) bit-for-bit; with the proposed
  production values (12/0.50) exactly {ATI, EME, BWXT} admit and
  {XLI, XLY} veto — RFC AC-a.
- (c) config matrix: both half-config directions + min_n∈{1, 100, 12.5,
  bool} + abs∈{0, 1.2, NaN, "0.5"} → ERROR log naming the key,
  bit-identical status-quo floor, nothing admitted on the 07-16 set;
  both-absent → silent status quo, no ERROR.
- (d) NaN exclusion: 12 finite + 3 NaN → guard inactive (n=12=N0);
  11 finite + 3 NaN → active; NaN still `veto:rank_score_nan`; None
  kept and uncounted.
- (e) small-n branch of each adaptive mode, incl. cap-mode relax no-op
  when cap (0.30) already sits below abs (0.50), and the n<2 stats
  fallback.
- (f) one-sidedness: both synthetic compressed sets (range 0.07 centered
  0.45/0.55) × 3 modes → guarded floor ≤ status quo, admitted superset;
  pathological cap(0.10) < min(0.20) → floor stays exactly 0.10, never
  raised (kernel + twin).
- Lockstep: twin floor/label/kept `==` kernel across modes × {recorded
  07-16/07-17, recorded n=85, both compressed sets} × guard on/off.

Full suite: 1818 passed, 8 skipped (baseline before this change:
1767 passed, 8 skipped — +51, zero regressions), via `make test` in a
CI-faithful py3.10 env (editable common/base-data/artifacts/pipeline +
xgboost).

## Fail-closed proof

Guard keys absent → floors bit-identical to today (tested at n=85 and
n=5). Invalid/half config → loud ERROR + bit-identical status quo, which
on small-n days means the recorded all-veto (fails toward no-entry).
The active branch is relax-only by construction — it can only widen
admission, never narrow it, and admitted names still face every
unchanged downstream gate (conviction μ floor, Kelly min-edge, QP,
correlation/sector caps). `RQ_SIM_BYPASS_BUY_FLOOR` untouched.

## Not in this PR (later stages, per RFC §3)

Strategy-104 config PR (`buy_floor_min_n: 12`,
`buy_floor_absolute_smalln: 0.50`) — nothing activates until it lands;
orchestrator degradation-sentinel rule (built-in N0_sentinel=12);
shadow verification session; pin bumps.
