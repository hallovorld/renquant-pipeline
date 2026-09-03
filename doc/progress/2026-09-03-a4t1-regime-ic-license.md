# P-REGIME-IC honours the RFC#210 A4-T1 license for the one authorized zero-trade candidate   (PR TBD)

STATUS:    delivered — time-limited, artifact-bound license; fails closed to the
           standing hard fail on every missing piece and after 2026-09-07.
WHAT:      `kernel/rfc210_license.py` gains `evaluate_a4t1_regime_evidence_license`
           (served only when: the RFC#210 freshness license is served AND
           `metadata.fallback_a4t1_override is True` AND
           `fallback_a4t1_candidate_run_id ∈ A4T1_LICENSED_RUN_IDS =
           {"20260831T141820Z"}` AND `today <= fallback_a4t1_expiry` AND the
           artifact carries the orchestrator's consumption receipt). Both
           P-REGIME-IC twins (`preflight._check_regime_layered_ic` and
           `preflight_pipeline.tasks.gate.RegimeLayeredICTask`) take the
           license ONLY on the "no eligible regime" branch and return a SOFT
           pass whose text leads with `LICENSED (RFC#210 A4-T1): regime-layered
           OOS evidence ABSENT` and names the candidate, the expiry and the
           receipt. Every other artifact, every other branch, and the window
           after expiry are byte-identical to before.
WHY/DIR:   2026-09-03 09:09 PDT the operator-authorized candidate
           20260831T141820Z was promoted under RFC#210 (bt#128 / orch#1110 /
           RenQuant#632). Its WF produced no round-trips, so `trade_monotonicity`
           carries no eligible regime and the 13:55 PT daily full run aborted
           at P-REGIME-IC [HARD] ("no regime has enough OOS trades") →
           sell-only fallback → no completed live run → rq105 export FAILED →
           shadow serving SKIPPED → "rq105 DOWN". The full run had already been
           aborting since 08-31 on P-WF-GATE (day-29 lapse); the promotion
           licensed that gate and exposed this one. The operator's authorization
           ("go" on the regime_sanity_ic bypass, 09-02) was to SERVE this model;
           the pipeline preflight re-checks the same evidence at a second
           layer, so the same time-limited license must reach it or the
           authorization is void in practice. Direction: G-C (model refresh path
           reaches an honest, SERVED outcome) with the risk stated in the text
           the operator reads every day: buys admitted WITHOUT regime IC proof.
EVIDENCE:  artifact:      `RenQuant/logs/daily_104/2026-09-03.log` lines 377 (P-REGIME-IC ✗ HARD) and 398-401 (PRE-FLIGHT FAILED, no orders); `logs/daily_104/2026-08-31..09-02.log` (P-WF-GATE ✗ HARD each day) [VERIFIED — read 2026-09-03 13:58 PDT]
           prod or exp:   prod preflight (both twins); admission of live buys for one artifact until 2026-09-07
           existing data: `tests/test_a4t1_regime_ic_license.py` 15 new tests (license shape, one run id, expiry boundary, receipt required, override literal True, RFC#210 underneath, malformed expiry; full-run SOFT+LICENSED text, unlicensed zero-trade still HARD, closed window HARD, no receipt HARD, sell-only unchanged, eligible-regime artifact never takes the license path; twin text + refusal parity) — with `test_rfc210_license` / `test_preflight_truth_text` / `test_preflight_wf_gate` / `test_wf_fail_override`: 82 passed [VERIFIED — 2026-09-03 14:20 PDT]
           best-known?:   no — the licensed artifact is a zero-trade candidate the standing policy refuses (genuine_ic 0.00155); this PR does not claim it has signal, it makes the operator's decision executable and self-expiring
           scope:         "this changes P-REGIME-IC for exactly one artifact digest/run id until 2026-09-07; it does not relax the gate, the config, or any other check"
NEXT:      umbrella pin advance (renquant-pipeline → this merge) + snapshot →
           live ff-only → `subrepo_assemble --sync` → the 13:55 PT daily104 on
           2026-09-04 runs FULL (expect `✓ P-REGIME-IC [SOFT] LICENSED (RFC#210
           A4-T1) …` in the log, orders placed). Companion: the orchestrator
           bundle checker accepts the same stamp as its `operator_authorized_override`
           for the missing Sharpe keys (DOCTOR bundle_consistency RED). After
           2026-09-07 the license closes itself; the real fix remains a
           candidate with round-trips (retrain recipe).
