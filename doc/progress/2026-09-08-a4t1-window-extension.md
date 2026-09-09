# A4-T1 window extension: one reviewed, digest-bound second window   (PR #TBD)

STATUS:    prepared — AUTHORIZATION PENDING. This does NOT merge until
           renquant-orchestrator LONG-ledger row 2h carries the operator's
           first-hand confirmation of exactly this artifact and this end date.
WHAT:      `kernel/rfc210_license.py` gains `A4T1WindowExtension` +
           `A4T1_WINDOW_EXTENSIONS` (one entry) and `a4t1_window_extension()`.
           The artifact's stamped `fallback_a4t1_expiry` remains the default
           and is NEVER rewritten; only when `today > stamped expiry` is the
           registry consulted, and an entry applies only when the run id
           matches AND the stamped `fallback_a4t1_candidate_digest` equals
           the entry's digest AND `today <= entry.until`. The served reason
           then says `… until 2026-10-16 — stamped window 2026-09-07 EXTENDED
           by renquant-orchestrator LONG-ledger row 2h`, and provenance
           carries `a4t1_window_extended`, `a4t1_stamped_expiry`, the
           authority and the reason, so the daily log states it every run.
           Everything else is untouched: RFC#210 underneath, the literal
           `True` override, the orchestrator receipt, and the standing
           close-by-itself behaviour with an empty registry.
           The shipped entry: run 20260831T141820Z, digest
           `760912ec…4af1e`, until **2026-10-16**.
WHY/DIR:   The 2026-09-07 expiry closed itself exactly as designed. From the
           2026-09-08 session P-REGIME-IC is HARD again ("no regime has
           enough OOS trades"), the daily aborts to sell-only, and with it go
           the shadow lanes and the rq105 export — 104 and 105 are both dark
           from one cause. There is no other servable artifact:
           * served (2026-08-31, the A4-T1 candidate): 0 eligible regimes;
           * previous (2026-08-02): 1 eligible regime but 37 days old
             against the 28-day SLA, so P-WF-GATE refuses it;
           * every candidate since 09-01 (09-03/09-05/09-06): 0 eligible
             regimes and genuine_ic 0.00024–0.00087, two orders of magnitude
             under the A4 floor of 0.02 — none would pass the gate even after
             the WF-sim repair (RenQuant#639), and the RFC#210 freshness
             fallback cannot promote them either while prod is 8 days fresh.
           So the buy path is structurally closed until either the operator
           grants a second window or a model with real edge appears; the
           second is research, not an ops fix. Extending in code rather than
           by editing the served artifact keeps the orchestrator's ledger
           receipt binding intact — rewriting the stamp would change the file
           the consumption record covers.
           Direction: G-C, with the cost stated plainly — buys resume WITHOUT
           regime-IC proof on a zero-trade model, which is what the first
           A4-T1 grant already accepted, for five more weeks instead of one.
EVIDENCE:  artifact:      `RenQuant/logs/daily_104/2026-09-08.log:394-400` — `PRE-FLIGHT FAILED … ✗ P-REGIME-IC: no regime has enough OOS trades` → `Full live trader hit preflight system failure — rerunning sell-only`; ntfy 13:55 `[sell-only] DECISION | no trade (no_candidates) … held=3 eq=$10,989`; 09-04 the same check read `✓ P-REGIME-IC [SOFT] LICENSED` [VERIFIED — read 2026-09-08 ~22:20 PDT]
           prod or exp:   prod preflight for exactly one artifact digest until 2026-10-16; admits live buys again once the served-pin fix (renquant-strategy-104#107) also lands — the pin blocks the blend load independently
           existing data: served/previous/candidate WF metadata read read-only (0 / 1 / 0 eligible regimes; genuine_ic 0.00155 / 0.00289 / 0.00024–0.00087; ages 8d / 37d) [VERIFIED — 2026-09-08 ~22:15 PDT]; `tests/test_a4t1_window_extension.py` (10 cases: the shipped entry, the 09-08 artifact served with EXTENDED text, inside-window unchanged, closes by itself at 10-16/10-17, digest bound incl. case-insensitivity and a truncated digest refused, run-id bound, no other refusal rescued, empty registry restores the close, both preflight twins) with `test_a4t1_regime_ic_license.py` + `test_rfc210_license.py` + `test_preflight_truth_text.py` + `test_preflight_wf_gate.py` = 55 passed [VERIFIED — 2026-09-08 between 23:05 and 23:20 PDT]; read-only evaluation against the LIVE served artifact + pinned config at today=2026-09-08: without the registry `REFUSED — A4-T1 window closed: expiry 2026-09-07 < today 2026-09-08`; with it `SERVED … until 2026-10-16`, `a4t1_days_left 38`, stamped expiry still 2026-09-07 [VERIFIED — same window, nothing written]
           best-known?:   no — the licensed artifact remains the zero-trade candidate the standing policy refuses (genuine_ic 0.00155). This PR does not claim it has signal; it makes a second operator decision executable and self-expiring
           scope:         "this extends ONE artifact digest's A4-T1 window to one dated end; it does not relax any gate, admit any other artifact, rewrite any artifact, or change what happens after 2026-10-16"
NEXT:      row 2h (orchestrator) must carry the operator's confirmation
           naming this artifact and this date — the operator may name a
           shorter date, which is a one-line amendment here. Then: row 2h →
           this PR → strategy-104#107 (the served-pin move, row 2g; buys stay
           blocked at the blend load without it) → umbrella pin advance +
           snapshot → live ff-only + `subrepo_assemble --sync` → the next dawn
           preflight reports 0 problems and the 13:55 run places orders.
           The real exit remains RenQuant#639 (the WF gate has crashed on
           every retrain since 09-01) plus a candidate with genuine edge;
           this window buys time for that, it does not substitute for it.
