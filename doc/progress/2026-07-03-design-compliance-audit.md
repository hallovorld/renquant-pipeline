# Progress — design-compliance audit memo (pipeline + strategy-104)

Date: 2026-07-03
Deliverable: `doc/audit/2026-07-03-design-compliance-audit.md` (docs only, no
code changes).

## What

Deep design-compliance audit of renquant-pipeline (the 104 decision core)
and renquant-strategy-104 (the config contract) against the umbrella
operating model (Universal Rules 1-6), both CLAUDE.md files, the #210
ownership table, and the standing rules (single-impl-imports-only,
default-OFF flags with byte-inertness tests, active==golden lockstep,
fail-isolated observe-only tasks). Six dimensions: primitive compliance,
ownership, hand-copied impls, flag hygiene, contract duplication, dead code.

## Result

**P0: 1 (latent) · P1: 24 · P2: 33.** Every P0/P1 verified first-hand at
file:line in fresh read-only clones (pipeline @ 778983a).

Top 5: (1) Kelly σ-horizon default 252 with preflight PASSING on the absent
key — re-arms the documented 2026-06-11 variance bug; (2) alpha158
train/serve parity is claimed but unenforced on the live XGB path; (3) the
observe-only shadow scorer can kill a live run via unwrapped umbrella-root
resolution; (4) forbidden-direction umbrella imports (`training.*`,
`transformer_v4`) that the boundary tests miss by construction; (5) the
divergent-default cluster (concentration cap 0.35-vs-0.12, staleness gate
off-on-absent-key, QP horizon contract warn-vs-strict, fifo-vs-hifo,
rotation/topup bars).

Also: flag-hygiene table for all 9 audited flags (2 clean-with-keys, 1
default-ON without a policy key, 3 dark flags missing their declared
strategy-104 keys); lockstep verdict healthy; 2 umbrella-only orphan config
keys; twin-module inventory (decision_trace x2, state_paths byte-identical
x2).

## Next

Sequencing proposal at the end of the memo — the §5 divergent-default sweep
and the five "now" items are small, high-leverage code PRs; training/scorer
relocation waits for the factory cutover; 4 strategy-104 config-key PRs.
