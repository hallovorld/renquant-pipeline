# 2026-08-04 — blend component kind dispatch (pipeline#260, GOAL-8 S1)

STATUS:    implemented + tested; awaiting review
WHAT:      `load_blend_scorer` gains per-component `kind` dispatch so the
           GOAL-8 S1 shadow profile can blend z(prod) + z(slow momentum).
           Absent `kind` (or `"panel"`) = the classic direct-artifact leg,
           byte-identical (the certified z(prod)+z(clf) profile carries no
           `kind` keys — covered by an identity regression). `kind:
           "momentum_residual"` loads through the ONE existing ledger-chain
           loader (`load_momentum_residual_scorer`: single-read snapshot →
           chain → tail dated artifact → sha both directions → row↔artifact
           parity → golden reproduction — nothing reimplemented). Any other
           kind fails closed (inverted default, no fall-through).
WHY/DIR:   The operator promoted the momentum+reversal blend to a goal
           (GOAL-8, 2026-08-03); S1 = z-blend shadow profile, and this
           loader gap was its single known code blocker (pipeline#260).
           Direction: pipeline first (the serving surface), then the s104
           blend-momentum profile + prereg freeze of the intersection
           semantic, then shadow deployment via the standard gates.
IDENTITY:  The momentum leg REFUSES `expected_content_sha256` (append-only
           ledger = byte pin stale by design; the same refusal the umbrella
           candidate-pin gate enforces on ledger pointers) and REQUIRES
           `expected_config_fingerprint` = the loader-stamped params
           fingerprint `momentum-<version>-<sha256(canonical params)[:16]>`,
           stable across weekly publishes with unchanged frozen params —
           so the composite config_fingerprint recipe is unchanged and
           stays stable week to week (verified by test).
SEMANTICS: Cross-section semantics UNCHANGED: NaN propagates through the
           component sum, so the composite scores the INTERSECTION of the
           legs' scored universes. The S1 prereg must freeze this
           explicitly before any run (documented in the module docstring).
           A chain-verified EMPTY fast ledger raises ShadowNotYetPublished
           out of the loader — fail-closed for a blend PRIMARY (the shadow
           lane's pending window does not apply to a composite).
EVIDENCE:  tests/test_blend_scorer.py 42 passed (4 new CI-runnable refusal/
           identity tests); tests/test_blend_momentum_component.py 5 passed
           (real construction via the model distro, importorskip on CI):
           happy path incl. intersection scoring + composite fp, fp
           mismatch, tampered chain, empty ledger, weekly-publish fp
           stability. Combined blend+momentum suites 83 passed. Full
           `make test` run recorded on the PR.
NEXT:      s104 blend-momentum shadow profile (delta on the momentum
           profile precedent: components + expected fps) + S1 prereg doc
           (intersection semantic + arms + placebo) + shadow lane
           deployment via the standard gates.
