# `fractional_max_book_pct` — S-FRAC v2 stage-3 AC #8, built (default OFF, inert today)

Date: 2026-08-28
Branch: `feat/fractional-max-book-pct` (renquant-pipeline, off `origin/main` e872440)
Design: renquant-orchestrator `doc/design/2026-07-02-s-frac-fractional-v2.md`
§3.3 (risk table, "hard cap on total fractional-sized exposure"), §3.4
(failure envelope: "the maximum aggregate fractional-position notional ever
unprotected by a broker-resident stop at any instant"), §6 stage 3 (AC + the
flag list: `fractional_shares.enabled`, `fractional_stops.day_belt_enabled`,
`fractional_max_book_pct`), §9.2 (open question: is 10% right).

## 0. Bottom line

- Before this PR the knob existed only in prose (design §3.4/§6; umbrella
  `doc/progress/2026-07-03-software-stops-layer.md:89`; pipeline
  `doc/progress/2026-07-03-s-frac-stage2-fractional-sizing.md:150`). No
  code in any repo read it [VERIFIED: `grep -rn max_book_pct` over the
  pipeline src, umbrella `backtesting/`, strategy-104 → 0 hits].
- Now: `execution.fractional_shares.max_book_pct` (default **0.10**) caps the
  post-trade fractional sleeve at `max_book_pct × PV` on all three
  buy-emitting kernel tasks. **Production is unchanged**: the flag is OFF in
  every strategy-104 lane, and with the flag off the cap function is never
  called (pinned by test, see §4).
- This is **step 4 of the 8-step S-FRAC enablement chain** (§5). Nothing is
  armed, flipped, or deployed by this PR.

## 1. Frozen contract (verbatim, as implemented)

> When fractional sizing is enabled and would emit fractional-sized BUY
> intents, cap the post-trade fractional sleeve at `max_book_pct ×
> portfolio_value`: existing fractional-sized positions (positions whose
> recorded quantity is non-integral) + new fractional intents ≤ cap. Intents
> beyond the cap are downsized to the remaining room, and if the room is
> below the broker minimum notional ($1 per the codebase's broker notes)
> they are dropped with a named skip reason `fractional_book_cap`.
> Deterministic order: process intents in the pipeline's existing emission
> order.
>
> When the fractional flag is OFF (today's production state) the code path
> is INERT: no behavior change, no new log lines beyond debug.
>
> Record the cap decision in the same per-ticker decision/skip surfaces the
> pipeline already uses for fractional.

Semantics settled from the design (both readings checked):

- **Total exposure, not new entries only.** §3.4 defines the cap as "the
  maximum aggregate fractional-position notional ever unprotected … at any
  instant, regardless of how many fractional names are held", and §3.3's
  loss bound is `cap × 20%` "independent of position count". So held
  fractional positions count toward the cap; new intents may only fill the
  remaining room. Implemented as such.
- **Config path.** The design names the knob `fractional_max_book_pct` but
  gives no path (§6 lists it beside `fractional_shares.enabled` as a bare
  name). The key lives **beside the other stage-2 keys** the pipeline
  already reads (`sizing.py:fractional_sizing_cfg` /
  `fractional_dust_floor_usd` / `fractional_eligible`):
  `execution.fractional_shares.max_book_pct`. FLAGGED: if the reviewer
  prefers the literal `execution.fractional_max_book_pct`, it is a one-line
  reader change plus test.

Conservative readings chosen where the contract left room (all FLAGGED):

1. **Drop threshold = the effective fractional floor, not bare $1.** The
   drop fires when the room (or the floored, downsized notional) is below
   `fractional_dust_floor_usd(config)` = `max($1 broker min, min_notional,
   min_fractional_trade_notional)` — $25 by default, never below the $1
   the contract names. Rationale: stage 2 already refuses any fractional
   entry below that floor (`fractional_dust_skip`); a downsized entry the
   floor would refuse is not made admissible by the cap. The pure function
   is tested on both the $1 and $25 floors
   (`test_apply_cap_drops_when_room_below_floor_named_reason`).
2. **Malformed cap = 0, and "malformed" is strict.** Only a real `int`/
   `float` in `[0, 1]` is accepted; `bool`, `str` (even `"0.1"`), `None`,
   NaN/inf, negative, or `> 1.0` fail CLOSED to 0.0 (no new fractional
   exposure) with a warning naming the value. A missing key is the only
   thing that yields the 0.10 default. Whole-share sizing never consults
   the key.
3. **Unknown exposure fails closed.** If a held fractional position has no
   finite positive mark in `ctx.prices`, the sleeve's size is UNKNOWN and
   every fractional intent that bar is dropped with `fractional_book_cap`
   (no entry-price fallback — an entry price can understate a rallied
   position).
4. **Rotation sell-legs still count.** On the rotation path the paired
   sell-leg's own fractional exposure (if any) is still counted — it has
   not sold yet when the buy-leg is sized.
5. **Top-ups are out of scope.** `TopUpHeldTask` has no fractional path
   today (stage 2 threaded only selection / rotation / joint); when it gets
   one it must call the same entry point.

## 2. Placement — why renquant-pipeline

Per the operating model (strategy-104 owns config values; pipeline owns
kernel sizing; umbrella owns adapters/commit contract; execution owns broker
validation — `configs/strategy_config.json` `fractional_shares._provenance`
in strategy-104 says exactly this):

- The cap is a **sizing decision** (how much of an admitted name, never
  whether) and needs three inputs that exist only on the pipeline's
  `InferenceContext`: the held quantities (`ctx.holdings[t].shares`,
  hydrated by the umbrella from broker `qty` — `adapters/runner.py:579-595`;
  read the same way by `task_rotation.py:1176` and
  `governor_sizing.py:449`), the marks (`ctx.prices`), and the intents
  already emitted this bar in emission order (`ctx.orders`, RotationJob →
  SelectionJob or JointActionJob, `pp_inference.py:583-592`).
- The umbrella's `commit_contract.py:190-226` is a capability preflight
  (flag on ⇒ broker contract + armed software stops, else fail-close all
  BUYs) and `runner.py:1476-1480` a cash-budget resize that only ever
  shrinks quantities — neither can raise an intent above the cap, so the
  pipeline-side cap holds through the commit path. Re-checking it there
  would duplicate the kernel sizing rule across the repo boundary.
- strategy-104 needs **no change for this step**: the reader defaults to
  0.10 when the key is absent. Adding the key to the config later is a
  strategy-104 PR that must also update the exact-key-set assertion in its
  `tests/test_strategy_configs.py:466-473`.

## 3. What changed

`src/renquant_pipeline/kernel/sizing.py` (pure helpers, self-contained):

- `DEFAULT_FRACTIONAL_MAX_BOOK_PCT = 0.10`, `FRACTIONAL_BOOK_CAP_SKIP_REASON
  = "fractional_book_cap"`, `FRACTIONAL_BOOK_CAP_DOWNSIZED =
  "fractional_book_cap_downsized"`.
- `fractional_max_book_pct(config)` — the strict reader (§1 item 2).
- `is_fractional_quantity(qty)` — finite, positive, non-integral (1e-9).
- `fractional_book_exposure(holdings, prices, orders)` — held non-integral
  quantities × `prices` + already-emitted intents tagged
  `sizing_mode == "fractional"` or carrying a non-integral `shares`;
  `None` when a fractional holding has no usable mark.
- `apply_fractional_book_cap(shares, price, *, cap_notional, exposure,
  floor_notional) → (shares, outcome)` — `≤ room` (1e-9 tolerance) is
  unchanged; otherwise floor-6dp to the room (never rounds up); room or
  floored notional `< floor_notional` ⇒ `(0.0, "fractional_book_cap")`.
- `cap_fractional_intent_to_book(...)` — the single entry point the tasks
  call, returning `(shares, outcome, info)`; `info` =
  `{cap_pct, cap_notional, exposure, room}` for the ledger.

Tasks — the call sits AFTER the stage-2 dust check and BEFORE emission,
inside the `use_frac and shares > 0` branch only:

- `task_selection.py` (SizeAndEmitTask): drop ⇒ `_block(ticker,
  "fractional_book_cap")` (⇒ `ctx._blocked_by_ticker[t]` +
  `selection_fractional_book_cap` counter, same surfaces as
  `fractional_dust_skip`); downsize ⇒ `fractional_book_cap_downsized`
  counter, order stamped `size_cap_reason: "fractional_book_cap"`
  (mirrors `size_floor_reason`) and `decision_inputs.fractional_book_cap =
  info`.
- `task_rotation.py` (EmitRotationsTask): drop ⇒ `ctx.rotations_blocked`
  row `{sell, buy, reason: "fractional_book_cap"}` and the ENTIRE pair is
  skipped before the exit is committed (no orphan sell, same shape as
  `fractional_dust_skip`); downsize ⇒ same stamps as selection.
- `task_joint_actions.py` (JointActionTask, `kind == "buy"`): drop ⇒
  `joint_fractional_book_cap` counter (mirrors `joint_fractional_dust_skip`);
  downsize ⇒ same stamps.

Log lines: `INFO … FRACTIONAL_BOOK_CAP …` on downsize/drop and a `WARNING`
on a malformed key — all inside the fractional branch, so a flag-off run
emits nothing new.

## 4. Test evidence

New: `tests/test_fractional_book_cap.py` — 29 tests, collected by CI:
`.github/workflows/ci.yml` runs `make test` = `pytest -q` with
`testpaths = ["tests"]` (`pyproject.toml:63-65`), so any `tests/test_*.py`
is picked up; no workflow names individual files.

| Requirement | Test |
|---|---|
| flag OFF inert | `test_flag_off_cap_path_never_invoked_and_byte_inert` — monkeypatches `cap_fractional_intent_to_book` to raise, runs 6 flag-off configs (incl. `max_book_pct` present / malformed), asserts orders, `_blocked_by_ticker` and counters equal the no-execution-block baseline and no `size_cap_reason`; `test_rotation_flag_off_never_invokes_cap` for the rotation path |
| cap exactly at boundary | `test_apply_cap_exact_boundary_is_unchanged` (pure; one cent over ⇒ downsized), `test_task_cap_exactly_at_boundary_leaves_intent_unchanged` (cap = intent/PV ⇒ 0.346363 unchanged) |
| downsizing | `test_apply_cap_downsizes_to_room_floored_never_rounded_up`, `test_task_downsizes_intent_to_remaining_room_and_stamps_ledger` (2% cap ⇒ 0.181818 sh, $199.9998 ≤ $200, `size_cap_reason`, counter) |
| drop below min notional, named reason | `test_apply_cap_drops_when_room_below_floor_named_reason` ($10 room: dropped on the $25 floor, downsized on the $1 floor; $0.50 room dropped on $1), `test_task_drops_below_floor_with_named_skip_reason` (held 0.9 sh @ $1,100 = $990 of $1,000 ⇒ BLK `fractional_book_cap`) |
| malformed config fails closed | `test_reader_malformed_fails_closed_to_zero_and_logs` (9 values), `test_task_malformed_cap_fails_closed_for_fractional_only` (BLK dropped; whole-share OXY still 7 `int` shares, no stamp) |
| existing fractional exposure counted | `test_exposure_counts_fractional_holdings_and_intents_not_whole_shares`, `test_task_existing_fractional_exposure_counted_integral_ignored` (0.5 sh counted, 2 sh / 3.0 sh ignored; 8% cap ⇒ 0.227272; over-cap ⇒ dropped) |
| deterministic emission order | `test_task_sequential_intents_consume_room_in_emission_order` (5% cap: 1st untouched, 2nd floored to the remainder, 3rd dropped) |
| rotation path | `test_rotation_buy_leg_downsized_and_dropped_by_book_cap` (default cap downsizes a $1,500 leg to $999.99; 20% cap leaves it; a prior $990 intent ⇒ whole pair skipped, no exit) |
| unknown mark | `test_task_unknown_mark_on_fractional_holding_fails_closed` |

Suite, run the way CI does (python 3.10, sibling checkouts of common /
base-data / artifacts / model source-installed, `pytest -q` from the
worktree):

- clean base `origin/main` e872440: **2673 passed, 8 skipped, 0 failed**
  (65.5 s) [VERIFIED].
- this branch: **2702 passed, 8 skipped, 0 failed** (28.0 s) = base + the
  29 new tests; targeted run of the fractional + rotation suites
  (`test_fractional_sizing_stage2.py`, `test_fractional_execution.py`,
  `test_rotation_prefilter_buy_leg.py`, `test_rotation_blocked_counters.py`)
  also green [VERIFIED].
- Pre-existing failures on the clean base: none.

## 5. Place in the S-FRAC enablement chain

The 8-step chain as the coordinating session enumerates it; steps 1-3 are
landed, this PR is step 4, steps 5-8 remain. [GUESS on the exact wording of
the enumeration — no committed doc spells out "8 steps"; the content below
is reconstructed from design §6 and the goal-1 closeout r2 plan
(`doc/research/2026-08-24-goal1-closeout.md:94-100` in the orchestrator).]

1. Stage 0 — umbrella commit contract, float-preserving + capability gate
   (`commit_contract.py`) — MERGED.
2. Stage 1 — renquant-execution#22 broker fractional order support,
   `MIN_FRACTIONAL_NOTIONAL_USD` — MERGED.
3. Stage 2 — pipeline fractional sizing + dust floor + KPI schema
   (pipeline#153 salvage, `2026-07-03-s-frac-stage2-fractional-sizing.md`)
   — MERGED, default OFF.
4. **Stage 3 AC #8 — `fractional_max_book_pct` cap (this PR).**
5. Stage 3 — software-stop layer ARMED + pager-on-missed-pass demonstration
   (§3.4 SLA) — built, unarmed.
6. Umbrella live broker adapter implements the execution#19 contract
   (`is_fractionable` + no-submit classifier) on the ACTIVE path — open
   (goal-1 closeout r2 item 2a).
7. Sim parity + ≥10-session frozen shadow packet + rollback drill (§6
   stage-3 AC) — open.
8. strategy-104 config: add `max_book_pct` beside the stage-2 keys, flip
   `enabled` under its own ledger row, pin advance — open; operator
   decision.

## 6. Not done / follow-ups

- No push, no PR, no strategy-104 config change, no umbrella change.
- Open question §9.2 (is 10% right; DAY-stop belt worth it) remains the
  operator's; the default here is the design's proposed number.
- The umbrella decision-ledger writer stamps whatever `decision_inputs`
  the order carries; `fractional_book_cap` info rides that path unchanged.
