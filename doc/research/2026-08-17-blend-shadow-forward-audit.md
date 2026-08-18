# Forward-shadow audit — the #213 blend readout record (2026-08-17)

STATUS: audit record (read-only investigation, 2026-08-17 ~22:44 PT). This file is the
committed evidence base for the superseding design
`doc/design/2026-08-18-blend-shadow-213-supersede-estimand-repin.md`. Every claim lists
its source; the audit performed no reads of matured outcomes (none exist) and no
production writes.

## Gate definitions (verbatim source)

`doc/design/2026-07-25-blend-shadow-deployment.md` (this repo, merged in PR #213,
merge commit 6ca46d11): primary statistic = paired per-session
`top10_spread(blend) − top10_spread(prod)`; reads ONLY at 60/120/+60N matured sessions
(no peeking; "any amendment = superseding design PR" §4); GATE rule = moving-block
bootstrap (block 20) 90% CI at 120 — lower>0 → GO to a NORMAL WF-promote submission,
upper<0 → KILL, else extend to a 240 cap then INCONCLUSIVE; winsorized ±50% guard;
<5% silent-skip feed precondition. Unparked by the model#74/#75/#76 chain; registered
decisive in renquant-orchestrator `doc/research/VERDICTS.md` row 2026-07-26.

Implementation semantics (renquant-orchestrator-run
`ops/renquant104/rq104_blend_readout.py`, clean vs its repo HEAD at audit time —
verified by git diff): session = one ledger row per run_date from the latest full live
run (≥80 candidate_scores rows); matured = ≥ MATURITY_TDAYS=61 later distinct sessions
in `ticker_forward_returns` AND all 20 picks carry non-null fwd_60d (all-or-nothing).

## Record state (sources: direct ledger read; SQL on runs DBs opened mode=ro)

- Ledger `RenQuant/data/rq104_blend_readout/ledger.jsonl`: **16 rows, 2026-07-27 →
  2026-08-17, 16/16 trading days, zero gaps**; all rows `"realized": false → matured
  sessions = 0**; silent-skip 0/16 (log grep, 0 ALARM lines).
- Lane DB `runs.alpaca_shadow_blend.db`: first date 07-28, 15/15 days, 2,447
  candidate_scores rows.
- Maturation feed live: `ticker_forward_returns` max updated_at 2026-08-17 20:55;
  fwd_60d current through as_of 2026-05-20 (= 61 trading days back).
- Calendar (trading-day arithmetic from the actual 07-27 first session, NYSE holidays
  applied): session #60 → 2026-10-19, matures ≈ **2027-01-15 (INFO@60)**; session #120
  → 2027-01-14, matures ≈ **2027-04-14 (GATE@120)**; 240-cap close ≈ 2027-10-05.

## Integrity findings

1. **Prod-arm identity flip 2026-08-04** (source: SQL pre/post on
   `runs.alpaca.db::candidate_scores` + ledger picks): active_scorer
   `panel_ltr_xgboost` → `blend` (momentum z-blend); recorded panel_score scale
   ~0.30 → ~2.9; `picks_prod` match the momentum-blend top-10 from 08-04 onward.
   Rows 07-27..08-03 (6) measure the model#76-certified contrast; rows 08-04.. measure
   z(z(xgb)+z(mom))+z(clf) vs z(xgb)+z(mom) — never screened, never preregistered.
2. **Horizon amendment without a superseding design PR in this repo**: fwd_20d→fwd_60d
   + MATURITY 21→61 on 07-29, orchestrator commit 690df5da; amendment doc
   renquant-orchestrator `doc/research/2026-07-29-blend-readout-horizon-amendment.md`
   self-flags its authorization as "not independently checkable"; this repo's design
   doc has zero post-merge commits (git log).
3. **Rows 07-27/28/29: unestablished clf-table attribution** — the locator's identity
   guard first executed 07-29 (orchestrator commit 0dcd5406, whose body records
   "unestablished provenance" deliberately).
4. **Clf artifact identity clean**: health JSONL
   (`RenQuant/backtesting/renquant_104/logs/shadow_scorer_health.jsonl`) shows
   `1e644354e0981f47` every session 07-28→08-17; on-disk artifact hashes to exactly
   that; 07-27 scored under the pre-restamp sha 99687a90 (model#83/#84 restamps,
   booster-byte-identical per their record).
5. Lane-config churn (git log, renquant-strategy-104
   `configs/strategy_config.shadow_blend.json`): 07-27 restamp pins, 08-03 raw-z
   threshold nulling, 08-04 component[0] pin rotation 04d7a381→6461b827 (RFC#210
   freshness), 08-06 cap 12→30%, 08-10 qp knobs. Affects the lane, not the ledger
   arms directly; vintages differ across the record (ordinary freshness churn).
6. Maturation-calendar hygiene: `ticker_forward_returns` history contains 6 phantom
   weekend dates + one missing weekday (2026-05-11), all pre-window; post-07-27 clean
   (SQL).

## What is computable now

Nothing statistical — 0 matured sessions and #213 authorizes no earlier read. This
audit reports only coverage + calendar + integrity, which its rules permit.
