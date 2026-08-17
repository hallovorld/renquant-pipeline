# 2026-08-17 — #289: pre-filter untradeable buy-legs before rotation pairing

## 1. Problem

Measured 2026-08-17 live (renquant_104): the rotation tree formed one pair
(CRWD→PANW), the long-signal guard blocked PANW at emit time
(`nonpositive_expected_return_no_long`, ER −0.0589), and the bare `continue`
never released CRWD back into the pool. With `max_rotations_per_bar=1`, the
bar's entire rotation budget was consumed by a trade that could never execute
— while GOOG (ER +0.0252, net_adv +0.1669 vs the same holding, both clearing
the 0.06 threshold) sat unused. Final: 0 rotations, `ECONOMIC_NO_TRADE`.

Root cause: eligibility (the long-signal guard, `EmitRotationsTask`) and
allocation (the greedy `used_holds` pair-finders, `BuildPairsTask`) lived in
different stages, and allocation was never revisited after a rejection.

## 2. Fix (issue option 2 — pre-filter)

`long_signal_ok_for_object(candidate, config)` is object-only — it reads only
the candidate's `panel_score` / `expected_return` / `mu` plus config, never
pair context — so eligibility can run before allocation.

`BuildPairsTask` now splits `eligible_candidates` through the guard
immediately after the held-set exclusion, BEFORE any finder runs. All three
finder modes (`find_rotation_pairs`, `find_thesis_primary_pairs`,
`find_thesis_symmetric_pairs` — all sharing the greedy `used_holds` shape)
consume that single list, so one filter point covers every path.

Observability parity (monitors read these surfaces):
- `ctx.rotations_blocked` row per pre-filtered candidate:
  `{"sell": None, "buy": <ticker>, "reason": <signal_reason>, "stage": "prefilter"}`
- same counter family as the emit-time guard: `ctx.counters["rotation_<reason>"]`
- `ctx._blocked_by_ticker.setdefault(ticker, reason)` (verified telemetry-only:
  order-attribution stamping, small-n eligibility report, score-distribution
  trace — never an admission input)
- INFO log per block keeps the `blocked rotation buy-leg` phrase for log greps.

The emit-time guard at `EmitRotationsTask` is untouched — it stays as a
normally-unreachable backstop (defense in depth). The bad-price pair check and
all other guards are untouched. No thresholds changed, no candidate reordering,
no finder internals changed.

Note: pre-filter blocks are ctx-level telemetry (summary `blocked=` count,
counters, blocked-by map). They are not persisted to the per-pair rotation DB
table — `record_rotations` keys rows on (sell, buy) and a pre-filtered
candidate has no pair; `_rotation_key_from_block` returns None on `sell=None`
and the row is skipped without error.

## 3. Tests

`tests/test_rotation_prefilter_buy_leg.py` (4 tests, all 4 FAIL on pre-fix
code, PASS post-fix):
- the 08-17 replay: measured 5-candidate table, held CRWD ER −0.1417,
  threshold 0.06, cap 1. Asserts pair=(CRWD→GOOG) forms AND emits
  (order + rotation exit), PANW's block recorded with the same reason string,
  counter `rotation_nonpositive_expected_return_no_long` == 4.
- N blocked candidates ranked above a passer, cap=1: capacity still available
  to the passer; N blocks recorded.
- all candidates fail: 0 pairs, all blocks recorded, no crash through
  Build→Validate→Emit, funnel bookkeeping intact.
- thesis mode: a pre-filtered candidate never reaches
  `find_thesis_primary_pairs` (recorder monkeypatch on the call site).

Backstop coverage preserved:
`tests/test_signal_direction_gate.py::test_rotation_buy_leg_uses_signal_direction_gate`
still exercises the emit-time guard on an injected pair.

## 4. Evidence

(a) Conclusion: the long-signal slot-burn is fixed at the pairing stage for
all three finder modes; a candidate the model refuses to buy can no longer
claim a holding's rotation slot.

(b)
- `artifact:` none — code + synthetic tests only
- `prod or exp:` neither — code + synthetic tests, no live run
- `existing data:` the issue's 2026-08-17 measured log evidence
  (`logs/daily_104/2026-08-17.log` `[VERIFIED]` in hallovorld/renquant-pipeline#289):
  PANW `→ swap` chosen=CRWD then `blocked rotation buy-leg —
  nonpositive_expected_return_no_long`; GOOG ER +0.0252 / net_adv +0.1669
  `available` chosen=NONE; `verdict=ECONOMIC_NO_TRADE fired=0`. The replay
  test reproduces this table synthetically and asserts the post-fix outcome.
- `best-known?:` honest scope — this fixes the long-signal slot-burn for all
  three finder modes (er / thesis_primary / thesis_symmetric). Pair-DEPENDENT
  blocks (bad price at emit, kelly_zero, insufficient_cash, preexisting_exit,
  and ValidatePairsTask's wash-sale/sector/correlation rejections) still burn
  slots — a blocked pair is still not re-paired. Named as remaining, out of
  scope for #289.
- `scope:` changes which candidates enter rotation pairing (intended behavior
  change per #289); no thresholds, no finder internals, no deploy — pin
  advance is a separate operator-gated step.

## 5. Files

- `src/renquant_pipeline/kernel/pipeline/task_rotation.py` — the pre-filter
  block in `BuildPairsTask.run`, after the held-set exclusion, before the
  mode fork.
- `tests/test_rotation_prefilter_buy_leg.py` — new.
