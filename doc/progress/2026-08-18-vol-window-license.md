# 2026-08-18 — vol-window buy license (orch#1004 impl PR 1, pipeline half)

STATUS:    delivered — code + tests complete; paired with the
           renquant-strategy-104 `shadow_vol_window` lane-config PR
           (cross-referenced in both PR bodies). NOTHING schedules or deploys
           this: the flag exists in no committed pipeline-consumed config
           until the s104 lane lands, the lane is wired into the daily-full
           only by impl PR 2 (orchestrator ops), and deploys stay
           operator-gated.
WHAT:      `kernel/panel_pipeline/vol_window_license.py` (new) + a flag-gated
           integration in the KERNEL `RegimeModelAdmissionTask`: inside the
           certified vol window (SPY vol20 > 0.135 STRICT ∧ resolved non-BEAR
           regime ∧ no hard-BEAR override), a lane that explicitly enables
           `ranking.panel_scoring.vol_window_license` keeps the TOP-DECILE
           (by served panel score) buy-admissible in place of the missing
           per-regime WF evidence; everything else gets the byte-identical
           pre-existing block path. Per-session JSONL ledger row (window
           state, licensed names, underlying refusal) via the house lane-log
           convention. Twin-pair re-pin (kernel-only) with justification
           entry, per the tools/twin_pairs.py contract.
WHY/DIR:   orch#1004 approved design §7 impl PR 1, authorized by the
           CONFIRMED vol-switch verdict orch#1003 (frozen prereg orch#1001):
           turn the certified ON-state top-decile spread (+0.184/60d, NW t
           +1.952, boot q05 +0.021, P2 block-t +2.378 `[VERIFIED — prior
           work, orch#1003 §1]`) into a SHADOW-lane mechanism that accrues
           the pre-committed activation evidence. Runtime admission
           machinery lives in renquant-pipeline.
EVIDENCE:  see §4 below (artifact / prod or exp / existing data / best-known?
           / scope — the §4(b) block).
NEXT:      impl PR 2 (orchestrator ops): daily-full lane wiring (readonly
           broker + `RENQUANT_READONLY_TAG=alpaca_shadow_vol_window`) +
           the AC3 activation-evidence readout (realized h=60 decisive,
           h=20 velocity-diagnostic) over the lane ledger + runs DB.
           Activation of anything on the production book = a separate
           operator decision (design §4 Stage A).

## 1. What the license is

The WF gate rightly refuses an unconditional bull license: the served
artifact's `wf_gate_metadata` has BULL_CALM `trade_monotonicity.passed=false`
and `sanity_regime_ic` passing only in BEAR `[VERIFIED — read from the served
artifact 2026-08-18]`. orch#1003 certified a CONDITIONAL: when SPY 20-td
realized vol > 0.135 (and only then), the panel's top-decile selection
carries a positive 60d spread. The license implements exactly that
conditional as an admission substitute — it fills the one empty slot
(missing bull regime evidence) and nothing else.

## 2. Frozen semantics implemented verbatim (design §2)

- ON ⇔ SPY 20-td realized vol (close-to-close simple returns, sample std
  ddof=1, annualized sqrt(252)) > 0.135 — STRICT, exactly 0.135 is OFF
  `[VERIFIED — orch#1001 prereg §2; orch#1003 runner realized_vol20]`.
  Computed PIT from `ctx.spy_returns`, the SAME series `BEAROverrideTask` /
  `HurstTask` / `CUSUMTask` consume, read-only.
- Window = ON ∧ ¬BEAR with ABSOLUTE hard-BEAR precedence: refused when
  `ctx.regime == BEAR` OR `regime_state.hard_bear`. Declared fail-closed
  narrowing: the regime must additionally be in the enumerated RESOLVED
  non-BEAR set (`BULL_CALM/BULL_VOLATILE/CHOPPY/BULL_STRONG`) — an
  unknown/unresolved regime carries no BEAR-precedence information and is
  refused. This can only narrow the window, never widen it.
- Top decile: N = int(round(n/10)) by served panel score
  (`ctx._panel_scores_all`, the RAW served cross-section) descending
  `[VERIFIED — orch#1003 runner top_decile_spread]`. Declared operational
  deviation: ties break on ticker (deterministic) rather than the runner's
  panel-row-order stable sort (also deterministic); recorded per session as
  `tie_break`.
- Governance precedence: the license substitutes ONLY for the
  trade-monotonicity / sanity-IC refusals. The diagnostic-only and wf-fail
  governed refusals are captured BEFORE the license (`governance_ok`) and are
  never overridden — test-pinned.
- Kill switch (design AC4): env `RENQUANT_VOL_WINDOW_LICENSE_DISABLE`
  (lane-scoped, set in the lane runner env) forces inactive; the session row
  still records `kill_switch=true`.

## 3. Integration shape (file:line anchors at this PR's HEAD)

- `src/renquant_pipeline/kernel/panel_pipeline/vol_window_license.py` — the
  whole mechanism: `spy_realized_vol`, `top_decile_by_score`,
  `evaluate_vol_window_license` (returns None BEFORE touching ctx unless the
  flag is exactly `true` — the unreachability contract),
  `emit_session_record` (fail-isolated JSONL append,
  `logs/vol_window_license.jsonl` under the lane strategy dir, mirroring
  `admission_shadow.jsonl` / `parking_sleeve_shadow.jsonl`).
- `src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py`,
  `RegimeModelAdmissionTask.run`: evaluation after the admission stages,
  before the block path; `_apply_vol_window_license` partitions the book —
  top-decile candidates kept, everything else gets the verbatim block-path
  treatment (blocked with the underlying refusal reason; non-licensed
  holdings exit-only); `ctx._full_candidate_snapshot` still snapshots the
  FULL pre-partition list so ConvictionGate's cross-sectional demean
  reference is unchanged; `ctx._regime_model_admission` records
  `ok:vol_window_license` + the underlying reason + the window summary
  (decision-trace compatible). Counters: `vol_window_license_sessions`,
  `vol_window_licensed_candidates`, plus the existing
  `regime_admission_blocked` / `regime_admission_holdings_exit_only` for the
  non-licensed remainder.
- `twin_pairs.json` + `twin_repin_exceptions.json` +
  `tests/test_twin_pairs_one_sided_repin.py` — the kernel-only re-pin with a
  justification entry (supersedes the pipeline#283 wf-fail entry per the
  file's own supersession rule). The public twin
  (`renquant_pipeline/panel_scoring.py::RegimeModelAdmissionTask`) is a
  different mechanism (`evaluate_model_admission`, no `wf_gate_metadata`
  regime-evidence stages) with no refusal slot to mirror the license into —
  same structural shape as the diagnostic-only and wf-fail lineage.

Downstream is untouched: sizing, caps, cash floor, tax, wash-sale, QP, and
the whole sell side see only a (possibly non-empty) candidate list exactly
where the block path would have handed them an empty one.

## 4. Evidence

(a) Conclusion: the license mechanism exists behind the lane flag, is
provably unreachable for any config that does not explicitly enable it, and
licenses exactly the certified top-decile inside the certified window with
absolute BEAR/governance precedence.

(b)
- `artifact:` none — code + synthetic tests only; the lane config lives in
  the paired renquant-strategy-104 PR.
- `prod or exp:` neither — flag-gated code no committed config enables on
  the pipeline side; the s104 lane config nothing schedules yet (wiring is
  impl PR 2; deploys operator-gated).
- `existing data:` design constants and their provenance are prior work:
  orch#1003 committed results (ON mean +0.18400, NW t +1.952, boot q05
  +0.02096, P2 +2.378; threshold 0.135; N=int(round(n/10)) construction)
  `[VERIFIED — orch#1003 results/runner, merged]`; the served artifact's
  BULL_CALM refusal shape `[VERIFIED — read 2026-08-18 from
  backtesting/renquant_104/artifacts/prod/panel-ltr.alpha158_fund.json:
  trade_monotonicity BULL_CALM passed=false; sanity BEAR passed=true]`.
- `best-known?:` honest scope — (i) byte-identity is proven at the ONLY
  mutated site: the task-level decision surface is compared against a
  VERBATIM frozen copy of the pre-change implementation across a 7-scenario
  prod-shaped grid, plus a raising-stub proof that the enabled path never
  executes without the flag, plus a call-site census pin (exactly one
  integration site). It is not an end-to-end artifact-serving replay — no
  test in this repo serves the real artifact stack, and the admission task
  is the only code touched. (ii) The session row records picks and window
  state; realized h=60/h=20 outcomes are the PR-2 readout's job. (iii) The
  tie-break deviation (ticker vs panel-row order) is declared above.
- `scope:` no behavior change for ANY existing config (test-pinned);
  new behavior exists only behind `vol_window_license.enabled=true`; no
  deploy, no pin advance, no scheduling — those are impl PR 2 + operator
  gates.

Suite: baseline at origin/main `763542b` = 2614 passed / 8 skipped /
2 FAILED (`tests/test_replay_d6_conventions.py::TestDefaultModeUnchanged` —
pre-existing pin-platform failures on this machine, identical before any
change `[VERIFIED — run 2026-08-18 on the unmodified worktree]`).
After this PR = 2659 passed / 8 skipped / the SAME 2 pre-existing failures
(+45 new tests, all passing) `[VERIFIED — make test 2026-08-18]`.

## 5. Files

- `src/renquant_pipeline/kernel/panel_pipeline/vol_window_license.py` — new.
- `src/renquant_pipeline/kernel/panel_pipeline/job_panel_scoring.py` —
  `RegimeModelAdmissionTask` integration (kernel twin only).
- `tests/test_vol_window_license.py` — new (45 tests: window computation /
  0.135 strict boundary / PIT / BEAR precedence / unreachability /
  byte-identity grid / licensed-top-decile-only / ledger).
- `twin_pairs.json`, `twin_repin_exceptions.json`,
  `tests/test_twin_pairs_one_sided_repin.py` — kernel-only re-pin +
  justification, per the twin-pair contract.
- `doc/progress/2026-08-18-vol-window-license.md` — this doc.
