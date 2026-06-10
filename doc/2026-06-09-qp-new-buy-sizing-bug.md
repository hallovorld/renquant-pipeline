# QP bug: new-buy target weights pinned <2% regardless of μ / γ / caps — handoff for codex

**Date:** 2026-06-09 · **Author:** Claude (handoff per operator) · **Status:** OPEN, blocking
**Severity:** HIGH — with `rotation.joint_actions.solver=qp`, the live primary can never
open a new position: every admitted buy solves to target_w < `qp_min_dw_pct` (2%) and is
skipped. Production is being temporarily reverted to the legacy path; QP must be fixed
before re-enable.

## 1 · Symptom (reproduced 6× on live readonly runs, 2026-06-09)

Pipeline state at the QP boundary is HEALTHY:
- preflight all-pass; pt07 (hf_patchtst primary) scores 89/89; 13 ranked candidates.
- QP admission passes for ~4 new names (e.g. HON, LLY, IBM, AMAT) + 3 holdings
  (EQIX, MU, ORCL).
- `ApplyKellySizingTask` (greedy-path parity reference) sizes the same candidates at
  **avg 5.9%** — so the signal supports real positions.

Yet the QP solution gives every NEW buy target_w ≈ 1.5% (< 2% min Δw) →
`EmitOrdersFromQPSolutionTask: skipped N trades below minimum Δw 2.00%` → `buys=0` every run.

**Holdings are sized correctly** (γ-responsive, hit the per-regime cap):
`QP_HOLDING_SOLVE EQIX/MU/ORCL: target_w=+0.0830` (= 0.12 cap × 0.69 confidence).

## 2 · What was ruled out (each by a live readonly run; logs in /tmp/validate_primary*.log)

| Hypothesis | Test | Result |
|---|---|---|
| risk aversion too high | `qp_risk_aversion` 3.0 → 1.0 → 0.25 (12×) | holdings scale correctly (0.027→0.088→cap); **new buys unchanged ~1.5%** |
| per-name cap | BULL_CALM `max_position_pct` 0.15→0.12, holdings hit it | buys nowhere near cap |
| sector cap | `qp_sector_cap_enabled=false` | **identical** solution (obj unchanged to 5 d.p.) |
| correlation cap | `qp_correlation_cap_enabled=false` (artifact is stale 2023-12-31 btw) | identical |
| forced deployment | `qp_min_invested_pct` 0 → 0.5 (edge_floor satisfied?) | identical — constraint appears not to engage |
| μ scale | enabled `ranking.alpha_to_mu` (Grinold-Kahn, IC=0.115) | transform fired (`raw_μ̄=0.0383 → μ̄_QP=0.0183`) — **buys still <2%**, obj went slightly negative |
| score floors | `min_panel_score` 0→null, ER floor 0.04→0.01 | exclusion reasons cleared (names admitted into QP universe) — sizing unchanged |

Key numbers from the GK run: candidates' raw μ̄ = **0.0383, σ_μ = 0.016** (these are the
calibrated `mu` attrs — NOT tiny). With μ≈3.8%, γ=3, σ≈0.30: unconstrained
w\* = (μ−κ)/(γσ²) ≈ 12% — the solver should saturate the cap. It returns ~1.5%.

**The new-buy weights are insensitive to μ (GK on/off), γ (12×), caps (on/off), and
min-invested — i.e. they are NOT on the mean-variance tradeoff surface. Something in the
QP problem construction pins new entries specifically.**

## 3 · Where to look (suspects, in order)

1. **Per-asset bounds for NEW names** in `ComputeQPConstraintsTask` and the soft-scaling
   tasks (`ApplyExposureScalingTask`, `ApplyConvictionCapTask` — the latter claims
   default-disabled; verify nothing else lowers `_qp_w_upper` for non-held names).
   Holdings vs new names diverge here: holdings hit `w_upper`=cap, new names act as if
   `w_upper`≈1.5%. **Check `_qp_w_upper` per ticker right before solve** — a
   `ConstraintSnapshot` dump (`constraint_snapshot.build_snapshot_from_ctx`) at solve time
   would settle this in one run.
2. **`_qp_dw_max` / per-trade Δw bound for new entries** — `qp_dw_max=0.5` global, but
   check for an entry-specific path (e.g. settled-cash/T+2 scaling, buying-power
   conversion) that shrinks the buy direction only.
3. **Cash/budget modeling**: settled_cash $6.6k vs equity $10.6k; check the cash
   constraint row — if buys are budgeted against some small fraction (e.g.
   reserve/settlement haircut applied twice), 4 names × 1.5% ≈ 6% ≈ suspiciously close to
   some haircut of settled cash.
4. **Cost model on the buy side**: `qp_cost_kappa=0.002` with
   `qp_cost_kappa_floor_round_trip=true` — verify the round-trip floor isn't being applied
   per-unit-weight in a way that dominates μ for new entries (κ_eff ≫ stated 40bps).
5. **`davis_norman` band** (`qp_band_method=davis_norman`): verify the no-trade band for
   w=0 names isn't producing an effective "stay at 0 unless w\*>band" with a mis-scaled
   band for new entries.

## 4 · How to reproduce (3 min, no orders)

```bash
# umbrella repo, config = backtesting/renquant_104/strategy_config.json
# (set rotation.joint_actions.solver back to "qp" + enabled=true first)
set -a; source .env; set +a
OMP_NUM_THREADS=1 .venv/bin/python -m live.runner --strategy renquant_104 \
  --broker readonly-alpaca --once --strategy-config-name strategy_config.json
# observe: QP_HOLDING_SOLVE at cap; "skipped N trades below minimum Δw 2.00%"; buys=0
```

The pt07 sidecar carries an operator-override `wf_gate_metadata` (2026-06-09) so all
preflight/admission gates pass — the repro reaches the QP cleanly.

## 5 · Acceptance criteria for the fix

1. With admitted candidates whose (μ, σ, caps) imply w\* ≥ cap, QP new-buy target_w
   reaches the per-regime cap (parity with holdings).
2. A regression test: solver given 1 holding at cap + 1 new name with identical (μ,σ)
   must produce symmetric weights (new-name-pinning is the bug class).
3. Live readonly run emits buys (Δw ≥ 2%) under the 2026-06-09 config.

## 6 · Current production state (so codex doesn't fight the environment)

- `rotation.joint_actions.solver` temporarily = `greedy` + `enabled=false` (legacy
  SelectionJob path) per operator go-live directive — revert to qp ONLY after this fix.
- Operator-override stamp on pt07 sidecar (both `metadata.wf_gate_metadata` and top-level
  `wf_gate_metadata` — note the dual-nesting consumers: preflight reads `metadata.`,
  runtime admission reads top-level).
- Config deltas of 2026-06-09 (all in `backtesting/renquant_104/strategy_config.json`,
  annotated `_*_20260609` / `_min_*_note`): `min_panel_score=null`,
  `min_expected_return_by_regime.BULL_CALM=0.01`, BULL_CALM `max_position_pct=0.12`,
  `ranking.alpha_to_mu.enabled=true` (IC 0.115; harmless on legacy path, re-evaluate on
  QP re-enable).

## 7 · Related

- renquant-model `doc/2026-06-09-patchtst-wf-gate-eval-bug.md` (gate-eval bug, separate)
- 2026-05-23 `_qp_min_invested_edge_reason` note (deployment previously disabled during
  admission repair — context for why cash sat idle)
- Stale correlation artifact `prod/watchlist-correlation.json` as_of **2023-12-31** —
  flagged during this investigation, needs refresh regardless.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
