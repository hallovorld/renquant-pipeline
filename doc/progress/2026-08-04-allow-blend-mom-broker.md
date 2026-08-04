# 2026-08-04 — ALLOWED_BROKERS learns alpaca_shadow_blend_mom (S1 session-1 crash)

Measured on GOAL-8 S1 session 1 (today's daily Step 5b): the shadow-blend-mom
run raised `ValueError: Unknown broker_name 'alpaca_shadow_blend_mom'` from
`_safe_broker` — the lane's profile/rail landed (RQ#563) but the single-source
allowlist here was never taught the new tag. Second same-day instance of the
"producer changed, a consumer allowlist wasn't" class (P-WF-GATE/RFC#210 was
the first).

One-line allowlist addition + regressions: tag accepted in BOTH state_paths
copies, `_safe_broker` round-trip, state/db files disjoint from
`alpaca_shadow_blend` and legacy `alpaca_shadow` (no suffix-prefix confusion).
Suites: broker-tags 25 passed (first draft of the disjoint test used an
invented API name and was corrected against the real `live_state_path` /
`runs_db_path` helpers).
