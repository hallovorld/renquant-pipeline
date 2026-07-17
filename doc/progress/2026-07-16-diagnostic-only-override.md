# Governed operator override for diagnostic-only buy admission

Date: 2026-07-16

## Problem

The 2026-07-15 panel-admission hardening (correctly) made
`wf_gate_metadata.diagnostic_only=True` an unconditional buy blocker at two
enforcement points — preflight `P-WF-GATE` and the scoring-path
`_diagnostic_only_admission` — with no authorization path. This conflicts
with the standing 2026-06-22 operator directive to trade the XGB scorer
under an explicit override while the WF-gate repair is in flight. After the
2026-07-16 pin sync deployed the hardening, the live book (already drained
to 94% cash by the week's serial exits) had no buy path at all: the weekly
promote chronically rejects (placebo sub-gate), so waiting for a genuine
non-diagnostic WF PASS means weeks of a frozen one-sided book. The operator
chose option (b): restore buy admission through a GOVERNED, auditable
override rather than an ungoverned bypass or a pin rollback.

## Governance contract

New config surface (strategy config, carried in a SEPARATE
renquant-strategy-104 PR — this repo ships only the validator/enforcement):

```json
"wf_gate": {
  "diagnostic_only_buy_admission": {
    "authorized": true,
    "operator": "renhao",
    "authorized_at": "2026-07-16",
    "expires": "2026-08-15",
    "scorer_model_content_sha256": "sha256:656b70be…",
    "reason": "…"
  }
}
```

Validated by the new shared module
`kernel/diagnostic_only_override.py::evaluate_diagnostic_only_override`:

- **Fail-closed**: absent block → refusal unchanged; ANY missing/malformed
  field, unparseable date, missing scorer identity, or unavailable hash
  implementation → refusal stands + WARNING naming the defect. A malformed
  authorization can never widen access.
- **Expiring**: `expires` required; `expires < today` is a hard stop
  (run trading date when the caller has one, else UTC today).
- **Scorer-content-bound**: the authorization names the schema-v1 content
  hash (`renquant_common.model_fingerprint.model_content_sha256`) of the
  ONE scorer it covers; preflight computes it from the loaded artifact
  payload, the scoring path uses the runtime
  `model_content_fingerprint_v1_recompute`. A retrained/substituted
  artifact does not inherit the authorization.
- **Audited**: full provenance (operator, dates, reason, bound + active
  hashes) is attached to the admitting check's `details` (preflight) and
  to `ctx._regime_model_admission` (scoring path), so it lands in the run
  bundle; a present-but-rejected authorization is also surfaced with its
  rejection reason.

## Enforcement points wired

1. `preflight_pipeline/tasks/gate.py` (`WfGateMetadataTask._evaluate_wf`,
   now receives the artifact payload): valid authorization → HARD PASS
   with provenance; invalid/absent → prior sell-only behavior, message
   names the rejection reason when a block was present.
2. `panel_pipeline/job_panel_scoring.py` (`_diagnostic_only_admission`,
   now receives config + trading date): valid authorization → admission
   `ok:diagnostic_only_operator_override` with provenance; the override
   lifts ONLY the diagnostic-only refusal — trade-monotonicity and
   sanity-regime admissions still apply when regime admission is enabled.

## Config-fingerprint invariant

`wf_gate.*` is outside `renquant_common.config_consistency
._model_relevant_fields` (which hashes only watchlist / panel_ltr /
sector maps / benchmark / resolution flags), so adding or expiring an
authorization never invalidates artifact config-consistency stamps.
Pinned by `TestConfigFingerprintUnaffected` in
`tests/test_diagnostic_only_override.py`.

## Tests

`tests/test_diagnostic_only_override.py` — 24 tests: validator fail-closed
matrix (absent / non-dict / each malformed field / unparseable dates /
expired / expiry-day-valid / wrong scorer / missing identity / happy path /
renquant-common hash path), both enforcement-point integrations (block
preserved without authorization, rejection reasons surfaced, admission with
provenance), and the config-fingerprint invariant. Existing
`test_preflight_wf_gate.py` + `test_panel_scoring_contract.py` unchanged
and green (default behavior identical when no authorization is configured).
