# 2026-08-04 — momentum identity as a dependency-light public contract (RQ#574 r3)

The umbrella's config-artifact-path gate validates a ledger component's
`expected_config_fingerprint` by recomputing it from the tail artifact's
params — but the recipe lived as a private function inside
`momentum_residual_scorer`, whose import transitively requires pandas, and
the pinned-path CI environment deliberately installs no heavy runtime deps
(codex round-3 finding on RQ#574).

`renquant_pipeline/momentum_identity.py` (new): stdlib-only public home of
the recipe (`params_fingerprint`). The scorer now imports it under its old
private alias — every stamp/caller unchanged, exactly ONE implementation.

Tests: recipe equality through both import paths; a clean-interpreter
subprocess proof that importing the module loads NO heavy dep (pandas/numpy/
xgboost/scipy checked by sys.modules); order-free canonicalization vector.
Related suites (momentum/blend/shadow-arm): 131 passed.
