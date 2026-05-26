# CLAUDE.md

Canonical operating model:
https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

Local repo map: `RENQUANT_REPOS.md`.

Branch policy: `main` is the stable interface consumed by other repos and
automation. Experiments, optimizations, and large upgrades happen on feature
branches, then merge back only after tests and integration checks pass.

## Repo Role

`renquant-pipeline` owns runtime inference, decision tree, QP/order-intent
generation, and full decision-trace persistence.

## Hard Boundaries

- Use `renquant-common` pipeline primitives for runtime flows.
- Model eligibility and alpha qualification happen before QP.
- QP may size/rebalance accepted candidates; it must not promote weak alpha.
- Do not train models or submit broker orders from this repo.
- Do not silently fallback to missing metadata, weaker scores, or stale
  artifact/data fingerprints.
- Large decision-policy changes use a feature branch.
- Do not delete or empty the source umbrella repo at
  `/Users/renhao/git/github/RenQuant`.

## Required Audit Surface

Decision traces must preserve enough fields to answer why each ticker was
accepted or blocked: model type, sector, score, blocked_by, QP delta, sell
P&L/tax/net, and relevant config/data/model fingerprints.

## Workflow

```bash
make test
make doctor
```
