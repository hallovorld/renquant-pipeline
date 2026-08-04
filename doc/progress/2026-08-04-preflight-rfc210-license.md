# 2026-08-04 — P-WF-GATE learns the RFC #210 serving license (sell-only incident fix)

## Incident (measured, live)

First day of the first RFC #210 freshness-fallback promotion: the ACTIVE panel
artifact carries `metadata.promotion_basis=freshness_fallback_rfc210` and —
by design — `wf_gate_metadata.passed=False` (the gate rejected the candidate;
the freshness governance served it anyway, renquant-backtesting#101/#102).

Today's daily-full run (2026-08-04 14:06 PT rerun) hit P-WF-GATE's
`passed is False` hard fail:

```
✗ P-WF-GATE: active panel artifact carries failed WF gate evidence:
  wf_sharpe_mean=0.6017... reason=FAIL: ... Refusing new live decisions ...
Full live trader hit preflight system failure — rerunning sell-only
```

The book executed sell-only (exits/risk controls ran; 0 buys). The intraday
lane, which does not run this preflight, served the new model normally from
12:36 PT. Root cause class: RFC #210 changed the promotion license, and this
consumer of `passed` was never taught the new license — the wrapper, sentinel
contract, and ack ledger were updated; the runtime preflight was missed.

## Fix

New `kernel/rfc210_license.py`: `evaluate_freshness_fallback_license(payload,
config, today)` proves the exact governance shape — basis string equal to
`freshness_fallback_rfc210` (metadata first, top-level fallback), parseable
ISO `trained_date`, age within the serving SLA (default 28d = RFC #210's own
number; `wf_gate.rfc210_max_served_age_days` overrides; the deployed
`model_staleness_days=60` split is orch#745's problem and deliberately NOT
consulted). Future-dated `trained_date` refuses. Fail-closed: any other shape
falls through to the existing hard fail with the refusal reason in details.

Both P-WF-GATE twins consult it before the full-run hard fail (the twin lesson
applied preemptively):
- `kernel/preflight.py` `_check_wf_gate_metadata` (the monolith the live
  runner resolves today), and
- `kernel/preflight_pipeline/tasks/gate.py` `WfGateMetadataTask`.

Sell-only behavior is unchanged on both. An admitted run carries
`details.freshness_fallback_rfc210` provenance (basis, trained_date, age_days,
max_served_age_days); a refused license carries
`details.freshness_fallback_rfc210_refused` with the specific reason.

## Verification

- `tests/test_rfc210_license.py` — 15 unit cases incl. SLA boundary (28d
  serves, 29d refuses), future date, bool-as-int override, metadata-decoy.
- `tests/test_preflight_wf_gate.py` — both twins: licensed full-run admits
  (hard/ok), aged-out refuses, unlicensed refuses, sell-only unchanged;
  monolith exercised end-to-end through a real artifact file.
- Preflight/gate selection: 372 passed, 5 skipped. Full suite run pending in
  this PR's CI.

## Deployment note (orchestrator side, not this repo)

The fix reaches the daily run only after the umbrella pin advances and the
runtime checkout syncs (merged-is-not-deployed). Tomorrow's 13:55 PT run is
the acceptance: expected P-WF-GATE hard PASS with rfc210 provenance and buys
unblocked (or an honest repeat of sell-only if the pin isn't advanced in
time).
