## What & why

<!-- Bottom line first: the conclusion and the decision needed. -->

## Evidence

<!-- Every number carries a provenance tag: [本次实测/早前实测/推导/假设]. -->

## Checklist

- [ ] `make test` green, count stated.
- [ ] Progress doc under `doc/progress/<date>-<slug>.md`.
- [ ] **Gate design rule (GOAL-5 AC6):** if this PR adds or tightens a HARD
      capital-admission gate — one that can take a name or the book from
      tradeable → not-tradeable via `raise` / zero-candidates / sell-only /
      buy-block, as opposed to a market decision — the progress or design doc
      states its **governed override path**:
      - **identity** — who lifts it, via what *reviewed* surface;
      - **expiry** — an explicit restore condition plus an auto-alarm.
        "Temporary" is not an expiry; "until X is deployed" is;
        - **binding** — scoped by fingerprint, with the override's provenance
        carried in the run bundle.

      A true kill-switch says so explicitly. **N/A if this PR adds no such gate.**

      Canonical rule: Universal Rule §7 in the umbrella
      `doc/arch/subrepo-operating-model.md`; rationale and worked examples in
      `renquant-orchestrator` `doc/design/2026-07-20-ac6-gate-design-rule.md`.

> **This checklist item is a review surface, not enforcement.** Nothing mechanical
> rejects a run bundle that omits override provenance today — measured
> 2026-07-31, `renquant-orchestrator` #690: the shared `LiveRunBundle` schema
> declares 7 fields and silently drops the rest, so a provenance field added to
> that path would be validated by nothing. Until that is fixed, this item and the
> reviewer reading it *are* the gate.

<!-- This repo owns the largest share of NON-TEST hard-gate code in the programme
     (src/+ops/: 89 files here vs 66 in renquant-orchestrator, measured 2026-07-31),
     which is why AC6 R2 landed here. Counting all *.py the order reverses, so the
     counting rule travels with the claim. See renquant-orchestrator#564. -->
