# S-FRAC stage 2 — fractional sizing under flag, supersedes A-3 when enabled

DATE: 2026-07-03
SCOPE: renquant-pipeline (sizing + buy-emitting tasks + sim/fake execution parity)
DESIGN: renquant-orchestrator `doc/design/2026-07-02-s-frac-fractional-v2.md`
§6 stage 2, §7.2 (supersession + fallback), §7.3 (anti-churn floor),
§7.4 (sizing-fidelity KPI schema), §7.5 (comparison arms).
CONSUMED CONTRACTS: stage 0 = RenQuant#439 (MERGED — float-preserving
`RunnerAdapter.commit`, capability gate, fail-closed entry absent the stop
layer); stage 1 = renquant-execution#22 (OPEN at time of writing — broker-side
fractional order support; `MIN_FRACTIONAL_NOTIONAL_USD = 1.0` mirrored here
with a parity note, since the pipeline cannot import the execution repo).
Stage ordering per §6 is 0 → 1 → 2 → 3; this PR is stage 2 CODE, default-OFF —
nothing activates until the stage-0 capability gate passes AND stage 3 arms
the software-stop layer, so landing it while #22 is still in review does not
reorder the enablement chain.

## What changed

1. **`kernel/sizing.py`** — the #153 salvage plus stage-2 additions:
   - `fractional_sizing_cfg` (fail-closed reader, salvaged AS-IS from the
     preserved #153 branch): only a genuine bool enables; YAML `"true"`/
     `"false"` strings, ints, None all fail CLOSED to whole-share mode.
   - `compute_position_size(fractional=, min_notional=)` (salvaged AS-IS):
     under the flag, the capped target deploys as `floor6dp(spend/price)` —
     floor, never round, so realized notional can NEVER round UP past the
     cap or available cash. Whole-share mode is byte-identical (regression
     re-pinned; the pre-quantization budget was factored into
     `sizing_target_notional` with bit-identical expressions).
   - NEW `sizing_target_notional(...)`: single-source §7.4
     `target_notional_i` (post Kelly/conviction/σ/PV, pre share-quantization)
     shared by the sizer and the ledger stamp — no hand-copied math
     (the calibrator-fingerprint triple-impl lesson).
   - NEW `fractional_dust_floor_usd(...)`:
     `max($1 broker floor [execution#22 parity], min_notional,
     min_fractional_trade_notional)`, default **$25**.
   - NEW `fractional_eligible(...)`: per-symbol fallback (§7.2) via config
     blocklist + optional ctx broker-metadata map; malformed blocklist fails
     closed to whole-share for every name. Sizing-time eligibility is
     advisory — the authoritative fail-closed fractionability check stays
     broker-side at submission (stage 1).

2. **`kernel/pipeline/task_selection.py`** (`SizeAndEmitTask`, the measured
   `size_insufficient_cash` drop site — BLK 07-01, BLK+AVGO 07-02):
   - §7.2 flag precedence: `fractional` (exact) → `one_share_floor`
     (round-up) → whole-share drop. The fractional path runs BEFORE the A-3
     branch; while the flag is on, the A-3 round-up is unreachable for
     fractionable names (`one_share_floor_roundups` → 0 for them — the
     monitorable supersession signal). A-3 remains the fallback for
     non-fractionable symbols and the flag-off state.
   - Both flags on ⇒ counted config warning
     (`config_warning_fractional_supersedes_one_share_floor`) + log.warning.
   - Dust guard: a sized fractional entry with `qty·price <
     fractional_dust_floor_usd` skips with the dedicated
     **`fractional_dust_skip`** reason (counter
     `selection_fractional_dust_skip`) — never a ~$0-invest admit.
   - §7.4 ledger fields stamped per order intent whenever a non-legacy
     sizing mode is configured: `sizing_mode ∈ {whole_share, one_share_floor,
     fractional}`, `target_notional`, `realized_notional_planned`
     (= shares × price at intent time — the plan-side numerator of
     `sizing_fidelity_gap = |realized − target| / target`; the fill-side
     realized notional is stamped by the stage-0 umbrella commit). With both
     flags off, order dicts stay byte-identical (same contract as A-3's
     `size_floor_reason`).

3. **`task_joint_actions.py` / `task_rotation.py`** — the other two
   `compute_position_size` consumers, threaded identically (#153) so a
   joint/rotation buy-leg cannot diverge from the selection path's sizing
   mode; same dust guard (dedicated counter / `fractional_dust_skip`
   blocked-pair reason); same §7.4 stamp when the flag is on; float-safe
   held-quantity reads for sell-leg proceeds.

4. **Sim/fake execution parity** (the #153 "rework — bridge to the ACTIVE
   path" item, scoped to what THIS repo owns): `OrderIntent.shares` accepts
   positive floats (bool/str rejected); `resolve_fill_quantity` capability
   negotiation — a fractional-capable backend models the float verbatim, a
   whole-share backend FAILS FAST (never silently floors to a zero-share
   fill); `SimBackend`/`FakeBackend(allow_fractional=True)` model the full
   fractional lifecycle (buy → partial sell → full liquidate → fp-dust clamp
   to exactly 0.0); `LeanBackend` stays whole-share and fails fast
   (LEAN backtest parity explicitly out of v2 scope, §5);
   `PrepareExecutionTask` fail-fasts a fractional config against a
   whole-share backend at the top of the bar. The ACTIVE LIVE path is the
   umbrella `RunnerAdapter.commit` — that contract is stage 0 (RenQuant#439,
   merged); this repo's execution tasks are the sim/shadow surface that
   stage 3's shadow replay consumes.
   Gated tightening vs #153: `_emit_qp_sell`'s float held/requested reads are
   now conditional on the flag — with stage 0 live, #153's unconditional
   `float(shares)` could have emitted fractional QP sell quantities in
   flag-off whole-share mode.

5. **Trims** (`task_trim.py`, salvaged, flag-gated): fractional trims floor
   to 6dp and respect the §7.3 anti-churn floor ($25 — trims are INCREMENTAL
   orders, the floor's designed home) instead of the $1 broker floor.

## Decisions recorded

- **Dust floor = $25 default** (`min_fractional_trade_notional`,
  `max`-composed with the $1 broker floor). Design §9.5 left the number
  un-ratified ("proposed $25"); stage 2 adopts it as the default, config-
  overridable. NOTE a deliberate scope extension: §7.3 scoped the $25 floor
  to INCREMENTAL orders with entries "unfiltered" — stage 2 also applies it
  to fractional ENTRY intents (with the dedicated `fractional_dust_skip`
  reason) on the argument that a sub-$25 fresh entry is the same taxable
  micro-churn §7.3 guards against. Flag for review; trivially revertible to
  broker-floor-only for entries.
- **`sizing_mode` vocabulary** = `whole_share | one_share_floor | fractional`
  exactly as §7.4/§7.5 specify.
- **Ledger stamp condition** = "any non-legacy sizing mode configured"
  (fractional OR one-share-floor), so Arm B (A-3) sessions carry
  `sizing_mode=one_share_floor` rows for the §7.5 frozen-cohort comparison
  while both-flags-off orders remain byte-identical.
- **Per-symbol fractionability** is advisory at sizing time (config
  blocklist + optional ctx map); the broker-side stage-1 guard remains the
  fail-closed authority.

## Tests (all green; full suite 1174 → 1220 passed, 7 skipped)

- Three-arm BLK fixture (the 2026-07-02 forensics case, $381 target @
  $1,100): whole-share ⇒ dropped; A-3 ⇒ 1 share = $1,100 (≈ +189% overshoot,
  ledger-visible); fractional ⇒ **0.346363 shares = $380.9993** (gap ≈ 2e-6,
  §7.4 stage-2 AC gap ≤ 1%).
- Flag-off byte-inert: absent/false/string-"true"/non-bool/empty configs all
  produce order dicts + block reasons identical to no-execution-block; no
  stage-2 field leaks; whole-share shares stay `int`.
- Dust guard: $15 target ⇒ `fractional_dust_skip` (never $0-invest);
  operator override to $10 admits; sub-$1 also skips.
- Admission invariance across all four flag states: same veto reasons
  (`negative_raw_signal_no_long` pinned), same admitted set, identical
  conviction/σ multipliers; only quantities differ.
- A-3 mutual exclusion: both flags on ⇒ fractional wins, roundups counter 0,
  config warning counted; non-fractionable fallback ⇒ A-3 rescues (or legacy
  drop without A-3); ctx broker-metadata `False` wins.
- 6dp floor never rounds up (property cases incl. the 66.666666̄ round-up
  trap) + salvaged #153 sizer/reader/negotiation/lifecycle suites.
- Sim parity round-trip: the ACTUAL SizeAndEmitTask-emitted order fills at
  exactly 0.346363 on `SimBackend(allow_fractional=True)`, full liquidation
  leaves zero residual; whole-share backend fail-fasts at
  `PrepareExecutionTask`.

## What stage-3 enablement still needs (this PR activates nothing)

1. renquant-execution#22 (stage 1) merged + pinned — broker-side fractional
   order support and the authoritative `MIN_FRACTIONAL_NOTIONAL_USD`.
2. RenQuant#440 software-stop layer merged, armed, and consumed by the
   stage-0 capability gate (fractional BUY stays fail-closed until then).
3. strategy-104 default-`false` config key (the #36 rewrite) + capability-
   contract test; flag flip only after the stage-3 shadow packet
   (≥10 frozen sessions, §6 stage-3 AC) and the operator's recorded
   risk acceptance (`fractional_max_book_pct`, pager SLA §3.4).
4. Shadow-replay sim parity re-pointed at whatever backend the stage-3
   shadow actually uses (§5 rework note); trims/QP float lifecycle is wired
   here but only exercised once fractional holdings can exist.
