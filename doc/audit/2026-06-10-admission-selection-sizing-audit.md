# Deep bug audit — ADMISSION + SELECTION + SIZING layer

**Date:** 2026-06-10 · **Auditor:** Claude (read-only audit) · **Scope:** decision-tree
admission gate, selection loop, position sizing only.
**Repo:** `renquant-pipeline` @ `main` (commit `221a328`).
**Reproduction interpreter:** `/Users/renhao/git/github/RenQuant/.venv/bin/python`
**Evidence DB:** `/Users/renhao/git/github/RenQuant/data/runs.alpaca.db`

Files in scope:
- `src/renquant_pipeline/kernel/pipeline/task_selection.py`
- `src/renquant_pipeline/kernel/selection.py`
- `src/renquant_pipeline/kernel/sizing.py`
- `src/renquant_pipeline/kernel/portfolio_qp/tasks.py` — admission gate helpers only
  (`_qp_buy_admission_block_reason`, `_qp_admission_*` floor/value helpers).
- Supporting reads: `kernel/kelly.py`, `kernel/regime.py::confidence_to_size_multiplier`,
  `kernel/panel_pipeline/job_panel_scoring.py::ApplyKellySizingTask`,
  `kernel/pipeline/task_rotation.py`, `kernel/pipeline/task_topup.py`,
  `kernel/portfolio_qp/job_qp.py`.

Out of scope (other auditors): scoring/calibrator, panel_veto, QP solve/emit numerics,
state persistence. Cross-cutting observations into those areas are flagged INFO only.

---

## Severity summary

| # | Severity | Title |
|---|----------|-------|
| B1 | **BLOCKER** | Per-regime knob silently falls through to global default (QP admission) — violates PRIME DIRECTIVE; disables ER-floor + horizon check in 3 of 4 regimes |
| B2 | **HIGH** | Oversize fallback in `compute_position_size` blows a Kelly/conviction-capped position to 22% for high-priced stocks (LLY 2026-06-10) |
| B3 | **HIGH** | Signal-direction gate enforced only in greedy `SizeAndEmitTask`; QP / rotation / top-up admit negative-panel (bearish) longs |
| B4 | **HIGH** | `min_panel_score=null` no-ops the QP panel-score floor while the gate's own `_reason` text still claims "require raw panel >= 0" |
| B5 | **MED** | `passes_sector_guard` over-blocks ALL new buys (any sector) if a single held ticker lacks a sector mapping |
| B6 | **MED** | `conviction_multiplier` floor/ceiling tuned for GBDT [0,1] scale; PatchTST panel scores (~-0.2) floor every conviction to `min_mult` — no dispersion |
| B7 | **MED** | conviction × sigma multipliers stack on top of Kelly target — double-counts μ and σ that Kelly already encodes (the `kelly_pure` opt-out defaults OFF) |
| B8 | **MED** | No freshness/TTL check on `corr_matrix` in the live selection/rotation path — a stale matrix is used silently |
| B9 | **LOW** | QP emit-loop slot-recheck env omits `admitted_new_tickers` (latent slot-accounting inconsistency) |
| B10 | **LOW** | Correlation-culprit diagnostic log uses the buggy `a or b` short-circuit (the guard itself was fixed; the log was not) and is not NaN-aware |
| B11 | **LOW** | Dead imports of deprecated binary `is_wash_sale_blocked` in rotation / joint_actions (not a behaviour bug) |

INFO (cross-cutting, other auditors own the fix):
- I1 — `veto:rank_score_below_floor` fired on higher-rank names while a lower-rank name
  (EQIX 0.5347) passed; driver is missing σ/NGBoost, not rank. (scoring/panel_veto area.)

---

## B1 — BLOCKER · Per-regime knob silently falls through to global default

**File:** `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:2487-2491`
(`_qp_admission_gate_value`), consumed by `_qp_admission_expected_return_floor`
(2494-2516), `_qp_admission_expected_return_over_sigma_floor` (2519-2545), and the
`max_sigma` cap (2433-2442).

```python
def _qp_admission_gate_value(gate, key, regime):
    by_regime = gate.get(f"{key}_by_regime")
    if isinstance(by_regime, dict) and regime in by_regime:
        return by_regime[regime]
    return gate.get(key)            # <-- silent fall-through
```

**Mechanism.** When a `{key}_by_regime` map exists but does NOT contain the live regime,
the code silently returns the *global* `gate.get(key)` (or `None` if unset). There is no
warning and no fail-closed. The PRIME DIRECTIVE requires per-regime resolution; this is
exactly the "silently fall through to a global default" anti-pattern.

**Production impact (real config).** The live gate in
`RenQuant/backtesting/renquant_104/strategy_config.json` sets:
```json
"min_expected_return_by_regime": {"BULL_CALM": 0.01}
```
with **no global `min_expected_return`**. Reproduced:

```
$ .venv/bin/python -c "from ...tasks import _qp_admission_expected_return_floor; ..."
BULL_CALM      -> ER floor = 0.01
BULL_VOLATILE  -> ER floor = None   (DISABLED - no floor!)
CHOPPY         -> ER floor = None   (DISABLED - no floor!)
BEAR           -> ER floor = None   (DISABLED - no floor!)
```

So in **3 of 4 regimes the expected-return floor is entirely OFF** — QP will size any
candidate with finite ER regardless of how thin/negative the alpha is.

**Compounding.** The horizon-mismatch consistency check (lines 2448-2455) is nested inside
`if er_floor is not None:`. When the floor falls through to `None`, the horizon check is
*also* skipped — so in BULL_VOLATILE/CHOPPY/BEAR a candidate whose `expected_return_horizon_days`
disagrees with the QP μ horizon is admitted unchecked too.

**Suggested fix.** Resolve per-regime with explicit fail-closed semantics: if a
`{key}_by_regime` map is present, a missing regime should NOT fall through to a permissive
global — either require the regime key (raise/log + use the strictest configured value) or
make the fallback explicit and logged. Decouple the horizon-consistency check from the ER
floor being set.

---

## B2 — HIGH · Oversize fallback blows a capped position to 22% for high-priced stocks

**File:** `src/renquant_pipeline/kernel/sizing.py:165-184` (`compute_position_size`,
the 25% fallback + min-1-share overrides), driven from
`task_selection.py:325-329` (`SizeAndEmitTask`).

**Mechanism.** The intended cap (`max_pct`, here the Kelly target) is converted to dollars
then to whole shares. For an expensive stock the cap buys 0 shares, so the function falls
back to **25% of portfolio**, and if that still rounds to 0, to **1 share**. Neither
fallback re-checks against `max_pct`/`max_concentration`. The greedy `SizeAndEmitTask`
(unlike the BEAR override path at `task_selection.py:330-333`) has **no post-fill cap
assertion**, so the oversize sticks.

**Evidence (LLY, run `2026-06-10-live-c2bf522c`, regime BULL_CALM, NAV $10,345).**
`trades` row: `kelly_target_pct=0.0823`, `panel_score=-0.125`, `mu=+0.035`,
`conviction=1.0`, `sigma_mult=1.0`, price $1137.88, **final `target_pct=0.22` (22%)**.

Reproduced exactly:
```
kelly cap 8.2% -> shares=2, actual_pct=22.0%
  intended dollars = 851, but 1 share = 1138 -> cap buys 0 shares
  oversize fallback 25% = 2586 -> 2 shares = $2276 = 22.0% of portfolio
cheap stock $50, same 8.2% cap -> shares=17, actual_pct=8.2%   (cap respected)
```

The dispersion is purely a function of share price, not conviction. A $50 name with the
same 8.2% cap stays at 8.2%; LLY at $1138 jumps to 22%. This is the operator's flagged
"one name >> others — sizing dispersion broken."

**Suggested fix.** After the fallback, cap shares so `shares*price/portfolio_value <=
max_pct` (or an explicit `max_concentration`). The "don't silently skip high-priced
stocks" goal can be met by allowing 1 share *only when 1 share itself is within the
concentration cap*; otherwise skip and log, rather than busting the cap by 2.7×.

---

## B3 — HIGH · Signal-direction gate enforced only in the greedy path

**File:** `src/renquant_pipeline/kernel/pipeline/task_selection.py:15-30, 253-274`
(`_require_positive_raw_signal_cfg` + the gate inside `SizeAndEmitTask`).

**Mechanism.** The 2026-06-10 "never long a bearish raw signal" gate
(`panel_score <= 0 -> block`) lives **only** in `SizeAndEmitTask`. Grep confirms no other
enforcement point:
```
task_selection.py:264  if bool(_require_positive_raw_signal_cfg(ctx.config)):
task_selection.py:273      _block(ticker, "negative_raw_signal_no_long")
```
The QP admission gate (`_qp_buy_admission_block_reason`), the rotation buy emitter
(`task_rotation.py:837-863`, `EmitRotationsTask`), and the top-up path
(`task_topup.py:220-296`) do **not** apply it. QP relies instead on `min_panel_score`,
which is null in production (see B4). So whenever QP is the active allocator, or a buy
arrives via rotation/top-up, a negative-panel (model-bearish) name can still be bought.

**Evidence.** Run `2026-06-10-live-c2bf522c` emitted 5 NEW_BUY orders, **every one with a
negative panel_score**:
```
LLY  panel=-0.125 mu=+0.035 target=22.0%
HON  panel=-0.123 mu=+0.036 target= 8.0%
GM   panel=-0.127 mu=+0.034 target= 7.7%
SPOT panel=-0.110 mu=+0.042 target= 4.9%
IBM  panel=-0.128 mu=+0.034 target= 2.6%
```
The entire 87-name candidate universe that day had negative panel_score (-0.04..-0.17)
with positive calibrated μ — the calibrator extrapolates a long thesis from a uniformly
bearish raw model. This run predates the gate commit (`894151f`, 21:11) and went through
the greedy path, so the new gate *would* now block it — but only on that one path.

**Suggested fix.** Hoist the signal-direction check into a single shared helper applied at
every buy-emission site (greedy, QP admission, rotation buy-leg, top-up), or enforce it in
`_qp_buy_admission_block_reason` independent of `min_panel_score`.

---

## B4 — HIGH · `min_panel_score=null` no-ops the QP panel floor; reason text lies

**File:** `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:2423-2431`.

```python
panel_floor = gate.get("topup_min_panel_score" if is_held else "min_panel_score",
                       gate.get("min_panel_score"))
panel = _source_float(source, "panel_score")
if panel_floor is not None:                      # null -> check skipped entirely
    floor = float(panel_floor)
    if not math.isfinite(panel) or panel < floor:
        return "qp_admission_panel"
```

**Mechanism + evidence.** Production gate has `"min_panel_score": null` and
`"topup_min_panel_score": null`, so the panel floor is a silent no-op — any negative panel
passes QP admission. Yet the same gate object's `_reason` field still asserts *"Require
calibrated rank >=0.55, raw panel >=0, and available slot"* and `_min_panel_score_note`
documents the deliberate null. The behaviour contradicts the human-readable contract. This
is the "floors that silently no-op when config is null" pattern, and the operator's flagged
"min_panel_score set to null so a whole-universe-negative model could still trade."

**Suggested fix.** This is partly intentional (PatchTST scores center ~-0.2, so a literal
0 floor would exclude everything). But the *direction* still matters: replace the null
no-op with the B3 sign gate (positive raw signal) or a percentile/relative panel floor, and
correct the `_reason` text so it no longer claims a floor that is disabled. Mark the rank
floor as the only live quality gate explicitly.

---

## B5 — MED · `passes_sector_guard` over-blocks all buys on one unmapped held ticker

**File:** `src/renquant_pipeline/kernel/selection.py:222-227`.

```python
for held in held_tickers:
    if held in defensive_set:
        continue
    held_sector = sector_map.get(held)
    if not isinstance(held_sector, str) or not held_sector:
        return False          # <-- blocks the candidate regardless of its sector
```

**Mechanism.** This validation loop returns `False` (blocks the candidate) the moment ANY
held ticker has a missing/blank sector — even though that held ticker may be in a totally
different sector than the candidate. One unmapped holding therefore blocks *all* new buys
in *all* sectors, not just its own.

Reproduced:
```
XYZ(Energy), held [AAPL(Tech), JNJ(unmapped)], max=2  -> False  (should be True)
XYZ(Energy), held [AAPL(Tech), JNJ(Health)],   max=2  -> True
```

**Real-world status.** Latent today: the current candidate pool has 0 null sectors
(`SELECT ... null_sector` = 0/87) and `sector_blocks=0` on recent runs. But `effective_held`
in `PrepareSelectionTask` can include positions that have drifted out of the watchlist /
`sector_map`, at which point this fires and silently halts all new entries.

**Suggested fix.** Skip held tickers with unknown sectors (treat as "not in candidate's
sector") instead of returning False, mirroring the `count` line below which already does
`sector_map.get(t) == sector` (a missing held sector simply doesn't match). The loop appears
to be a misplaced strict-validation that should be a `continue`, not a `return False`.

---

## B6 — MED · conviction_multiplier scale mismatch (GBDT vs PatchTST)

**File:** `src/renquant_pipeline/kernel/sizing.py:62-108`; defaults floor=0.0,
ceiling=1.0, min_mult=0.5.

**Mechanism.** The rescale `(panel_score - floor)/(ceiling - floor)` assumes panel scores
in roughly [0,1] (GBDT era). PatchTST raw panel scores center near -0.2 and the whole
universe is frequently negative (see B3 evidence). Every negative score maps to
`frac <= 0 -> min_mult`, so all names collapse to the same 0.5 conviction — zero conviction
dispersion across the book.

```
panel_score=-0.125 -> conviction=0.500
panel_score=-0.100 -> conviction=0.500
panel_score=+0.000 -> conviction=0.500
panel_score=+0.050 -> conviction=0.525
```

The code comments (`task_selection.py:281-288`) document a reverted "Issue 17" fix that
identified the same calibrated-vs-raw scale tension. Partly known; still a live correctness
gap when `panel_scoring.sizing.enabled` is on with PatchTST.

**Suggested fix.** Make floor/ceiling regime-/model-scale aware, or feed conviction off a
cross-sectional percentile of panel_score rather than the raw value, so dispersion survives
a negative-centered model. (Note: in the LLY run `sizing` was disabled, conv=1.0, so this
particular incident was unaffected — but the knob is a foot-gun if enabled.)

---

## B7 — MED · conviction × sigma stack on top of Kelly (double-counting μ, σ)

**File:** `src/renquant_pipeline/kernel/pipeline/task_selection.py:289-307` (and the
identical pattern in `task_rotation.py:844-863`).

```python
conv  = conviction_multiplier(panel_score, sizing_cfg)   # ~ f(μ proxy)
sig_m = sigma_multiplier(sigma, sigma_median, sigma_cfg)  # ~ 1/σ
...
max_pct = float(c.kelly_target_pct) * conv * sig_m        # kelly already = μ/σ²
```

**Mechanism.** `kelly_target_pct` already encodes μ/σ² (`kelly.py:94`). Multiplying it again
by a conviction term (monotone in panel_score ≈ μ proxy) and a sigma term (≈ 1/σ)
re-applies the same μ and σ Kelly already used. Because both multipliers are clamped to a
max of 1.0, the net effect is always a *shrink* (it cannot blow up), but it is still an
unprincipled double penalty on exactly the names Kelly already sized down — distorting
relative sizing. The `kelly_sizing.disable_extra_multipliers` (`kelly_pure`) flag neutralises
this but defaults **False**, so the stacked path is the production default.

**Suggested fix.** When `kelly_sizing.enabled`, default `disable_extra_multipliers=True`
(pure Kelly), or explicitly document that conv/σ are intended *additional* risk haircuts and
re-derive them from quantities orthogonal to μ/σ². In the LLY run both were 1.0 (sizing/sigma
cfg disabled), so no live damage there — but the stack is wrong by construction.

---

## B8 — MED · No freshness/TTL gate on `corr_matrix` in the live path

**Files:** `task_selection.py:92` (passes `ctx.corr_matrix` straight through);
`selection.py:456-471`, `task_rotation.py:687`, `task_joint_actions.py:705` consume it.

**Mechanism.** The leakage guard `assert_correlation_no_leakage` (used by QP and preflight,
`tasks.py:323-328`) only enforces `as_of <= backtest_start` (no future leak). There is **no
maximum-age / staleness check** anywhere on the live selection/rotation correlation path —
a matrix stamped, e.g., 2023-12-31 would be used silently for 2026 decisions. The artifact
writer does stamp `as_of_date` (`pp_training.py:683-689`), so the data to enforce a TTL
exists; it is simply never checked on the consume side.

**Suggested fix.** Add a `correlation_max_age_days` check at load (mirroring
`model_staleness_days` in `job_universe.py`): if the matrix `as_of` is older than the TTL,
fail-closed (block correlated buys / warn loudly) rather than trusting a stale matrix.

---

## B9 — LOW · QP emit-loop slot recheck omits `admitted_new_tickers`

**File:** `src/renquant_pipeline/kernel/portfolio_qp/tasks.py:3036-3057` builds the env for
`_emit_orders_loop`; it sets `emitted_new_tickers=set()` but never sets
`admitted_new_tickers`. `_qp_buy_admission_block_reason:2399` reads
`env.get("admitted_new_tickers", set())` (→ empty in the emit path).

**Mechanism.** Slot accounting in the emit loop therefore counts only
`held_after_exits | emitted_new`, missing the admitted-but-not-yet-emitted set. In practice
`BuildSourceMapTask._select_new_candidates_for_slots` (`job_qp.py:247-278`) already trims
candidates to open slots upstream, so the emit-loop recheck is largely redundant and the gap
is not currently exploitable. Still a latent inconsistency: the two call sites of the same
gate use different env shapes.

**Suggested fix.** Populate `admitted_new_tickers` in the emit-loop env (or remove the slot
recheck there and rely solely on the upstream selection), so the gate behaves identically at
both call sites.

---

## B10 — LOW · Correlation-culprit diagnostic log uses the buggy `or` short-circuit

**File:** `src/renquant_pipeline/kernel/selection.py:465-466`.

```python
corr = (ctx.corr_matrix.get(c.ticker, {}).get(held)
        or ctx.corr_matrix.get(held, {}).get(c.ticker))
```

**Mechanism.** This is the exact `0.0 or X` short-circuit that the guard body itself
(`selection.py:263-265`) was fixed to avoid: a real `0.0` forward correlation is discarded in
favour of the reverse-direction lookup. It is also not NaN-aware (unlike the guard). Pure
diagnostic string — it can mislabel *which* held name caused a correlation block but does not
change the decision. Cosmetic.

**Suggested fix.** Reuse the guard's explicit-None lookup and `math.isfinite` handling for the
culprit search so the log matches the decision logic.

---

## B11 — LOW · Dead imports of deprecated binary `is_wash_sale_blocked`

**Files:** `task_rotation.py:643`, `task_joint_actions.py:168` import
`is_wash_sale_blocked` but both actually call `is_wash_sale_blocked_with_cost`
(`task_rotation.py:662`, `task_joint_actions.py:678`). The binary import is unused. Not a
behaviour bug — noted because the deprecated function still exists and could be mis-wired by a
future edit. NOT-A-BUG / cleanup.

---

## Items checked and found NOT to be bugs (intentional)

- **Wash-sale consistency (selection vs QP).** Both `run_selection_loop`
  (`selection.py:434`) and the QP mask (`tasks.py:3504-3518`) call the same
  `is_wash_sale_blocked_with_cost` with `expected_dollar_return=None`, so both soft-block
  losses identically. The prior "binary vs cost-aware inconsistency" is resolved. NOT-A-BUG.
- **`confidence_to_size_multiplier`** floors at 0.5 (`regime.py:368-393`); LLY's
  conf 0.686 → 0.686 cap multiplier is the intended floored behaviour, not a bug.
- **Tier index clamping** (`selection.py:420`, `min(slots_filled, len-1)`) correctly reuses
  the strictest (last) tier once slots exceed the ladder length. NOT-A-BUG.
- **NaN/inf hardening** across `sizing.py`, `selection.py`, `kelly.py` (multiple documented
  audit fixes SIZ-1, CPS-1, S-1, SL-1/2, K-1, SELF-CORR) is sound and well-guarded.
- **Self-correlation skip** (`selection.py:261-262`) correctly avoids the corr(X,X)=1.0
  self-reject. NOT-A-BUG.

## INFO (other auditors)

- **I1** — In run `2026-06-10-live-c2bf522c`, `veto:rank_score_below_floor` blocked NVDA
  (rank 0.5399), TSLA (0.5448) etc. while EQIX (rank 0.5347, lower) passed with no veto. The
  vetoed names all have `sigma=NULL` (NGBoost did not produce σ); EQIX has σ. So the veto is
  σ-availability-driven, not a rank-floor inconsistency. This sits in the scoring/panel_veto
  auditor's scope.
