# Progress: small-n guard eligibility-ledger amendment (P0 remediation step 2)

Date: 2026-07-18

## What

`doc/design/2026-07-18-smalln-guard-eligibility-ledger.md` — amendment
to the approved small-n guard RFC (#204), accepting in full the P0 + P1
from the independent codex review on RenQuant#498: (1) a
reason-partitioned eligibility ledger as a HARD precondition (the
relax-only branch acts only on a CLEAN partition — approved-normal
exclusions, zero scorer/data/feature/manifest/coverage failure
residue; suppression is LOUD); (2) partition + floors + candidate delta
persisted to run bundle AND decision ledger every session; (3) a
shadow-first experiment contract with frozen affected-session
definition, baseline-vs-guarded deltas, replay corpus digests, four GO
criteria and explicit NO-GO triggers; (4) production activation only
after frozen shadow verdict + operator authorization on the record +
a new pin PR superseding #498.

## Status

RFC amendment only — no implementation, no config change. Keys sit
shadow-only (strategy-104#61).
