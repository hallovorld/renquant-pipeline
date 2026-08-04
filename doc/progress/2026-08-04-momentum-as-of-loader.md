# 2026-08-04 — the as-of verified momentum loader (the orch#783 provider boundary)

STATUS:    provider-boundary addition + a verbatim extraction refactor
WHAT:      codex on orch#783 ruled that the S2 readout must not duplicate
           ledger/artifact verification in orchestrator: pipeline imports
           the model-owned verifier and is the canonical artifact
           consumer, so the as-of primitive belongs HERE. Steps 3-6 of
           the serving contract (dated artifact, content sha both
           directions, row-artifact kind/cutoff/params parity, golden
           reproduction) are extracted VERBATIM into
           _verified_row_scores — the serving loader now calls it
           (behavior-preserving: its suite unchanged green) — and the new
           load_momentum_artifact_as_of selects the serving row for a
           session TIME-SAFELY (last chain-verified row with
           cutoff_date<=D and appended_at_utc<=D's cutoff; single-read
           snapshot discipline identical to serving) then runs the SAME
           extracted contract. Returns (scores, identity triplet incl.
           row_index/row_sha/artifact sha) or None on any gap — the
           readout counts coverage, never guesses.
EVIDENCE:  momentum handler + blend suites 87 passed (3 new as-of tests:
           time-safe selection with package-resealed timestamps via
           ledger.row_sha256_of, contract failures -> None, no
           qualifying row -> None); full suite 2409 passed with the SAME
           2 machine-local replay-d6 platform-pin failures as clean main
           (control run 2026-08-04 morning; none introduced).
NEXT:      orch#783's readout drops its local chain code and calls this;
           pin advance rides the S1 umbrella batch or its successor.
