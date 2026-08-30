# 2026-08-30 — P-WF-GATE / P-REGIME-IC never print a bare ✓ for a licensed or relaxed gate

**Bottom line:** with the served panel artifact (trained 2026-08-02,
`wf_gate_metadata.passed=false`, `promotion_basis=freshness_fallback_rfc210`,
`fallback_genuine_ic=+0.00289`) and the pinned strategy config's
`wf_gate.sanity_regime_ic_required=false`, the daily preflight logged

```
✓ P-WF-GATE   [HARD] active panel artifact is governance-served under RFC#210: … — buys admitted while the freshness license holds.
✓ P-REGIME-IC [HARD] regime-layered IC/monotonicity passed for eligible regimes ['BULL_CALM']; pooled_spearman=0.039…
```

while the stamp says the WF gate **FAILED** and BULL_CALM **FAILED**
(ρ=0.002) and the sanity IC **FAILED** — the log lied to anyone reading it
as a pass. [VERIFIED — served artifact + pinned config read 2026-08-30;
message text from `kernel/preflight.py:566-575, 766-771` and
`preflight_pipeline/tasks/gate.py:180-189, 417-423` on `origin/main` afb7362.]
The admission decisions are **unchanged**; only the ✓ text is. Both twins
now say, byte-identically:

```
✓ P-WF-GATE   [HARD] LICENSED: WF gate FAILED, genuine_ic=+0.0029, served age 26d ≤ 28 (promotion_basis=freshness_fallback_rfc210, trained 2026-08-04; wf_sharpe_mean=0.6017…, reason=FAIL: …) — buys admitted ONLY while the RFC#210 freshness license holds; this is not a WF pass.
✓ P-REGIME-IC [HARD] RELAXED: sanity IC failed (regime sanity IC failed: BULL_CALM,BULL_VOLATILE,CHOPPY); stamp failed BULL_CALM ρ=0.002; sanity_regime_ic_required=false — regime-layered IC/monotonicity NOT proven for eligible regimes ['BULL_CALM']; pooled_spearman=0.039…
```

Companion: RenQuant `fix/buy-blocked-alert-truth` makes the wrapper's
BUY-BLOCKED alert urgent and self-explaining for the day the license refuses
(2026-08-31, 29 d > 28). This PR reaches the 13:55 run only through a pin
bump.

## Change

* `kernel/rfc210_license.py`: `genuine_ic_from_payload(payload)`
  (`metadata.fallback_genuine_ic`, then the stamp's
  `sanity_placebo_genuine_ic`, else `None` → printed `n/a`, never invented)
  and `licensed_check_message(license, wf, payload)` — the single builder
  both P-WF-GATE twins call, so the text cannot drift between them.
* `kernel/preflight.py`: the licensed branch of `_check_wf_gate_metadata`
  uses the builder; new `_regime_ic_pass_message(details, eligible, failed,
  pooled_spearman)` leads with `RELAXED:` whenever
  `details["sanity_regime_ic_relaxed"]` / `["trade_monotonicity_relaxed"]`
  is set (each failed eligible regime with its stamped ρ), and keeps the
  plain "passed for eligible regimes" text for a genuine pass.
* `preflight_pipeline/tasks/gate.py`: `WfGateMetadataTask` and
  `RegimeLayeredICTask` call the same two builders (bridge import, like the
  other shared helpers).
* Severity/ok/details for every branch: unchanged. Sell-only, aged-out
  refusal, wf_fail_override and diagnostic_only paths: untouched.

## Tests

`tests/test_preflight_truth_text.py` (new, 9), fixture = the served
artifact's metadata shape: licensed text leads with the FAILED gate + genuine
IC + age-vs-SLA; genuine-IC precedence and `n/a`; Task twin and monolith twin
produce the identical LICENSED line; aged-out (29 d) still hard-fails with the
refusal and no `LICENSED`; a genuinely passed gate keeps
`WF gate passed: …`; relaxed P-REGIME-IC leads with `RELAXED:` in both twins
(identical text) with `stamp failed BULL_CALM ρ=0.002` and
`sanity_regime_ic_required=false`; strict config still blocks; a genuine
regime pass keeps the plain text.

Targeted (`test_preflight_truth_text`, `test_preflight_wf_gate`,
`test_rfc210_license`, `test_wf_fail_override`, `test_preflight_pipeline_gate`):
**67 passed**. Full `make test` equivalent (`pytest -q` with the worktree on
PYTHONPATH): **2728 passed, 11 skipped, 2 failed** — the 2 are
`tests/test_replay_d6_conventions.py::TestDefaultModeUnchanged::{test_default_evidence_matches_pre_change_pin,test_default_evidence_byte_identical_on_pin_platform}`,
which fail identically on untouched `origin/main` (stash/unstash, verified
2026-08-30; replay-evidence byte identity, unrelated to preflight).

## Umbrella consumers

`RenQuant/scripts/daily_104.sh` and `scripts/check_readonly_e2e.sh` grep the
check **names** (`P-WF-GATE`, `P-REGIME-IC`); names, `✓/✗` markers and the
`✗ P-*:` failure format are unchanged. The umbrella kernel copy
(`backtesting/renquant_104/kernel/preflight.py`, known-drift allowlisted) gets
the same P-REGIME-IC relaxed text in the companion PR; it has no RFC #210
path.
