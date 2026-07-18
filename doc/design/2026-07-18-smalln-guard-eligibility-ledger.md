# Design amendment: eligibility-ledger precondition for the small-n guard

Date: 2026-07-18
Status: RFC amendment to `2026-07-17-vetoweakbuys-smalln-guard.md`
(approved #204) — design review required before implementation.
Owner: drafted personally per design-review policy.
Origin: P0 + P1 from the independent codex review on RenQuant#498
(review 4727377226), ACCEPTED in full. The base RFC's guard keys only on
the COUNT of finite-scored candidates; it cannot distinguish a healthy,
intentionally narrow cross-section from scorer/data/feature failure
residue — the exact case where relaxing admission must not happen.

## 1. Amendment scope

- §2.1's relax-only floor formula, config surface, bounds, and
  fail-closed matrix are UNCHANGED.
- NEW hard precondition (§2 below): the small-n branch may act only on a
  CLEAN eligibility partition.
- NEW persistence contract (§3): the partition and floor decision are
  recorded in the run bundle and decision ledger.
- NEW shadow-first experiment contract (§4) with an explicit NO-GO —
  production activation is gated on its frozen verdict plus operator
  authorization on the record.
- Key placement: production + golden configs carry NO activation keys
  until §4 completes (strategy-104#61); the daily shadow config carries
  them now.

## 2. Eligibility partition — the clean-scan precondition

At VetoWeakBuysTask entry, build the partition for the current scan.
Most fields come from surfaces that already exist in the funnel; TWO
require new instrumentation (r2): the generation-stage
`expected_universe` counter and the promoted feed-staleness marker —
both named in §2's CLEAN definition and part of the implementation
scope:

| field | source |
|---|---|
| `expected_universe` | watchlist ∩ session eligibility (pre-candidate count from the candidate-generation stage) |
| `entered_scan` | candidates entering panel scoring |
| `scored` | candidates with a panel score (ApplyScoresTask counters) |
| `score_missing` | `panel_score_missing` drops (`_drop_unscored_panel_candidates` counters) |
| `nonfinite` | scored but NaN/inf rank_score (the existing `veto:rank_score_nan` class) |
| `pre_floor_exclusions` | per-reason counts from `ctx._blocked_by_ticker` upstream of the floor (wash-sale, vol-gate, override-ineligible, etc.) |
| `finite_n` | the n the guard sees |

**CLEAN means ALL of (r2 — review round 1 P1s incorporated):**

1. **Mass balance (P1-1).** `entered_scan + Σ(recorded pre-scan
   exclusion counts) == expected_universe`, where `expected_universe`
   is a counter EMITTED BY THE CANDIDATE-GENERATION STAGE at run time
   (watchlist ∩ session eligibility, recorded before any drop). Any
   unaccounted shortfall — the signature of generation-starving
   failures that leave no per-name records (bars-feed outage, the June
   per-ticker staleness shape) — is NOT CLEAN. If the counter is
   absent (older pipeline), expected_universe is UNKNOWN → NOT CLEAN.
   This REVISES the r1 claim that no new upstream instrumentation is
   needed: the generation-stage counter is required implementation.
2. **Funnel integrity:** `score_missing == 0` AND `nonfinite == 0`.
3. **Approved-normal reasons WITH share bounds (P1-2).** Every
   `pre_floor_exclusions` reason belongs to the explicit allowlist
   frozen in config, AND each INTEGRITY-classed reason's count share of
   `expected_universe` is within its config-frozen bound (proposed
   defaults: wash-sale ≤ 20%, realized-vol gate ≤ 50%, corporate
   action ≤ 10%) — a mass of bogus tags under an approved class (the
   RenQuant#428 STATE-EXT-SELL wash-sale precedent) breaches its bound
   → NOT CLEAN. POLICY-classed narrowing (governed-override
   eligibility, membership rules) is exempt from share bounds because
   the narrowing is itself declared in reviewed, pinned config — but
   it must still be the EXACT reason string the config declares.
4. **No failure markers:** no scorer/calibrator/feature/manifest/
   coverage failure marker on the context. v1 detectable set (all
   existing surfaces): `_panel_scoring_contract_failed`,
   `panel_score_missing` counters, `veto:rank_score_nan`,
   fingerprint-dispatch errors. Feed-staleness currently logs a
   warning with NO machine surface (r2 honest scope): implementation
   PROMOTES that warning to a context marker
   (`_feed_staleness_flagged`), and until that marker exists the
   staleness class is covered indirectly by (1)+(3) only — stated
   plainly rather than claimed.

- CLEAN and `finite_n < N0` → the relax-only branch MAY act (§2.1 of the
  base RFC, unchanged).
- NOT CLEAN → the branch MUST NOT act: floor stays status quo (which
  fails toward no-entry), and the run is tagged
  `smalln_guard_suppressed(reason=<first failing class>)` — LOUD in the
  sentinel (a suppression on a small-n day is exactly a day a human
  should look at). **Named deliverable (r2):** a separate orchestrator
  PR extends the deployed #545 sentinel with a
  `smalln_guard_suppressed` LOUD pattern — the current rule fires only
  on all-veto∧small-n and would miss a suppressed-but-partially-
  admitting day.
- The partition is computed REGARDLESS of whether the branch fires, so
  normal-n days build the same record (baseline data for §4).

The 07-16/07-17 sessions are the motivating positive case: n=5 arose
from governed-override eligibility (approved-normal), zero score_missing
— CLEAN, guard acts. A feature-axis collapse yielding 5 survivors would
show `score_missing > 0` — NOT CLEAN, guard suppressed.

## 3. Persistence (P0(b) verbatim)

Per session, persisted to BOTH the run bundle and the decision ledger:
the full partition (§2 fields + per-reason exclusion counts),
`finite_n`, `N0`, `original_floor`, `relaxed_floor` (equal when the
branch did not act), `branch_action` (acted / not-small-n / suppressed
+ reason / deconfigured), and `candidate_delta` (tickers admitted by
relaxation that the status-quo floor would have vetoed — empty unless
acted). Schema versioned; absence of the block in older bundles is
explicit (`smalln_ledger: absent`).

## 4. Shadow-first experiment contract (the second P1)

- **Affected-session definition (frozen):** any session whose shadow-arm
  partition is CLEAN with `finite_n < N0`. Non-affected sessions are
  controls.
- **Comparison:** for every session, baseline (status-quo floor) vs
  guarded (relax-only) computed IN THE SAME shadow run from the same
  partition record: candidate sets, order intents, turnover, modeled
  costs, and downstream-gate outcomes (conviction/Kelly/QP kill counts
  on the delta names). Replay corpus: the recorded small-n sessions
  (07-16/07-17 + the 14-session set from #543) with corpus digests
  frozen in the verdict.
- **Upstream-failure exclusion:** suppressed sessions are reported but
  excluded from the efficacy comparison (they are the guard NOT acting,
  by design).
- **Verdict (frozen before evidence):** GO requires, over ≥ N_shadow=10
  affected shadow/replay sessions: (i) zero suppression-logic errors
  (no session where manual audit finds the partition mislabeled); (ii)
  every delta name traceable through downstream gates with no risk-gate
  bypass; (iii) the guard admits ≥1 name on ≥70% of OPERATIVE affected
  sessions — operative = the absolute bound sits BELOW the status-quo
  floor, i.e. relaxation can change the outcome; compressed-scale
  sessions where the relax-only `min()` degrades to status quo are
  EXCLUDED from this denominator (r2 — they are one-sidedness
  correctness evidence, and counting them would NO-GO the guard for
  behaving correctly); (iv) no new alarm class fires.
  **NO-GO if:** any partition mislabel (a failure-residue day classified
  CLEAN — the P0's core hazard) OR any delta name bypasses a downstream
  gate OR the sentinel small-n rule fails to fire on a suppressed day.
  NO-GO → back to design, no production keys.
- The machine sync and shadow verification are recorded by the
  orchestrator against exact pins (run-surface records, per the P1).
- **Production activation sequence after GO:** frozen shadow verdict
  committed → EXPLICIT operator authorization on the record → new pin
  PR (superseding RenQuant#498) with keys restored to production +
  golden. Neither agent may self-authorize this step.

## 5. Acceptance criteria (amendment)

- AC-A: replaying 07-16/07-17 partitions → CLEAN, branch acts, delta =
  {ATI, EME, BWXT} (consistent with base-RFC AC-a).
- AC-B: synthetic failure-residue day (score_missing > 0 at n=5) →
  suppressed, status-quo floor, LOUD tag; sentinel fires.
- AC-C: the partition block appears in the run bundle + decision ledger
  on EVERY session (normal-n included), schema-versioned.
- AC-D: suppression reasons enumerate every failure surface named in §2;
  an unknown/unclassifiable exclusion reason → NOT CLEAN, and an ABSENT
  expected_universe counter → NOT CLEAN (fail-closed on missing records,
  not just recorded-unknown ones).
- AC-E: the shadow verdict artifact contains the frozen corpus digests,
  all four GO criteria evaluated, and the NO-GO triggers checked.
- AC-F (mass balance): a synthetic generation-starved day (expected 145,
  entered 5, zero recorded exclusions) → NOT CLEAN, suppressed, LOUD —
  even though every within-funnel record looks healthy.
- AC-G (share bound): a synthetic mass-wash-sale day (wash-sale share >
  bound at small n) → NOT CLEAN, suppressed; the same day under the
  bound → CLEAN.
