# Wash-sale materiality floor (`risk.wash_sale.materiality_floor_usd`) — inert at default 0

**Date:** 2026-08-02 · `renquant-pipeline` · pipeline#223 · governing contract:
renquant-strategy-104 `doc/design/2026-08-02-wash-sale-materiality-floor.md` (merged)

## Bottom line

This PR implements the governed materiality floor the s104 design specifies and
**changes nothing at runtime**: the knob defaults to 0.0/absent, the entire new
code path short-circuits there, and a floor-0 A/B on a synthetic session fixture
asserts decisions AND log messages byte-identical to a baseline expectation
derived from the unchanged detection function
`[VERIFIED — tests/test_wash_sale_materiality_floor_usd.py, 45 tests]`.
Full suite: **2337 passed, 9 skipped, 2 failed — the exact same 2
`test_replay_d6_conventions` pin-platform failures fail identically on pristine
`origin/main` (2292 passed / same 2 failed), i.e. pre-existing, not a
regression** `[VERIFIED — both suites run 2026-08-02 on this machine]`.

## AC6 posture (this IMPLEMENTS a governed loosening)

The override triplet (identity / expiry / binding) lives in the merged s104
design; this PR is step 2 of its rollout order. **Nothing changes at runtime
until (3) pins advance on both repos AND (4) an explicit non-zero floor lands by
reviewed config PR in renquant-strategy-104** (the design proposes starting at
$5 `[ASSUMED — design's proposed initial operator floor]`). No env-var, no CLI
override; the pipeline refuses values above the $50 design ceiling as a contract
violation (floor DISABLED + loud finding — never a clamp).

## What the contract says, and where each clause landed

* **Knob + fail-closed validation** — `resolve_wash_sale_materiality_policy`
  (`kernel/selection.py`). Absent → 0.0, no finding. A PRESENT non-number
  (incl. quoted numbers and bools), negative, NaN/inf, or > $50 value →
  floor DISABLED (0.0) + a loud `config_validation_finding` record; an invalid
  `assumed_marginal_rate` (default 0.40, ceiling 1.0) ALSO disables the floor —
  a bad value never waives anything, not even at a substituted default.
* **Zero-floor short-circuit is normative** — every call site guards with
  `blocked and floor > 0.0`; the `estimate <= floor` comparison is never
  evaluated at floor 0. The design's constructed case (a name whose estimate is
  exactly $0.00 at floor 0, via an explicitly configured rate 0.0) asserts the
  block still fires.
* **Estimate** — `estimate_foregone_wash_sale_tax_benefit_usd`:
  event-net disallowed loss × assumed marginal rate, `Decimal` ROUND_CEILING to
  the cent (the floor systematically UNDER-fires). The disallowed loss is the
  SAME-EVENT-NETTED realized P/L: `ctx.last_sell_pls[ticker]` is the event-net
  FIFO P/L by construction, and lot-detail callers go through
  `kernel/portfolio.py::event_net_realized_pnl_from_disposed_lots`, which
  routes the EXISTING netted lot engine (`compute_disposed_lot_tax`, netting
  fix 2026-07-27) — no new tax math; the gain-lots-without-netting defect
  class is refuted by test with numbers (gain +50 / loss −80 nets to −30 →
  est $12.00, not the losses-only $32.00).
* **Waive / stand / unavailable** — `WashSaleFilterTask`
  (`kernel/pipeline/task_candidates.py`): estimate ≤ floor → the name proceeds
  and the decision-trace record `{gate: "wash_sale", ticker, waived: true,
  est_foregone_tax_usd, floor_usd, config_fingerprint}` is staged; estimate
  UNAVAILABLE (missing/non-finite P/L — the MU case) → block STANDS, stamped
  `[estimate_unavailable]` on `blocked_by`. Detection logic
  (`is_wash_sale_blocked_with_cost`) and `wash_sale_days` untouched.
* **Run-bundle surface** — `collect_wash_sale_decision_records`
  (aggregated in `pp_inference.py` Phase 2b) →
  `ctx.wash_sale_decision_records` → appended into the `decision_trace` that
  `runtime_inference_payload` / `live_context_snapshot_from_live_context`
  emit and `build_native_live_bundle` collects. The records ride a separate
  ctx attribute so the explicit-vs-built trace branch is untouched; absent
  attribute (the default) leaves the payload byte-identical.
* **Mass-block aggregate semantics** — waiving is PER-NAME: a waived name
  never sets `blocked_by`, so `_wash_sale_count` counts STANDING blocks only
  (`estimate_unavailable` blocks still count). Mixed sessions can lower the
  count below `min_count`/p99 but never flip a standing block; if EVERY
  blocked name waives, `wash_sale_mass_block` does not fire — each waive is
  individually accounted for by its bundle record. Documented at the
  aggregation site (`task_funnel_integrity.py`) and tested both ways.
* **`config_fingerprint`** — sha256 of the raw `risk.wash_sale` subtree
  (`sha256:<16 hex>`), deliberately NOT the model-relevant
  `fingerprint_config` (which would not change when the floor changes): the
  stamp changes iff the reviewed policy subtree changes, which is the AC6
  attribution requirement.

## Why four call sites, not one

The candidate gate (`WashSaleFilterTask`) is the choke point that zeroed
sessions and the single record emitter — but a candidate it releases is
re-checked by §1091 logic at `task_joint_actions.py`, `task_rotation.py`
(ValidatePairsTask), and the QP mask (`portfolio_qp/tasks.py::
_compute_qp_wash_mask`, which forces Δw ≤ 0 — a waived candidate entering the
QP at weight 0 could never be bought). All three now honor the SAME waiver
arithmetic under the same `floor > 0` short-circuit — otherwise the floor
would be inert scaffolding on the live path. The greedy `run_selection_loop`
(non-prod legacy) and the parking sleeve are deliberately NOT wired,
mirroring the #227 NPV-floor precedent (the sleeve's foregone return is
~risk-free; an unconditional block there is defensible).

## Interplay with the existing `wash_sale_min_material_npv` knob

Independent by construction: the NPV knob releases names INSIDE the detection
function (they are never blocked, so the floor never sees them); this floor
waives names the detection function blocked. A config carrying both behaves
compositionally `[VERIFIED — test_npv_floor_and_materiality_floor_do_not_interfere]`.

## Evidence run on the issue's own session shape

Driving the real gate on the 2026-07-28 table from pipeline#223 (8 names)
`[VERIFIED — end-to-end drive, 2026-08-02]`:

| config | standing blocks | waived |
|---|---|---|
| floor absent (today) | 8 (identical to the incident) | 0 |
| floor $5 / rate 0.40 | 4 — CSCO (est $5.47), FTNT ($10.18), CRWD ($195.38), MU (`estimate_unavailable`) | MCHP $0.52, BWXT $1.26, NEE $1.30, AFRM $4.09 |

CSCO standing at est $5.47 > $5.00 shows the ceil-up boundary doing its
designed under-firing.

## Twin-pairs re-pin

The two public payload builders (`runtime_inference_payload`,
`live_context_snapshot_from_live_context`) changed deliberately, so their
`twin_pairs.json` public digests were regenerated via `tools/twin_pairs.py
--emit` (2 digest lines changed, nothing else) and are committed through this
review, per that file's own contract.
