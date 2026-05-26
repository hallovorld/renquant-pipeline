# renquant-pipeline

Runtime decision-pipeline repository for RenQuant.

Operating model: https://github.com/hallovorld/RenQuant/blob/main/doc/arch/subrepo-operating-model.md

Repository map: [RENQUANT_REPOS.md](RENQUANT_REPOS.md)

Local automation:

```bash
make test
make doctor
```

This repo owns inference/runtime composition: preflight, regime gates,
candidate scoring, ranking, selection, rotation, QP, order-intent generation,
and decision-trace persistence. It consumes model artifacts through contracts;
it does not train models and does not submit broker orders.

## Runtime Decision Contract

Panel scoring is fail-closed. Runtime must provide an artifact feature contract,
per-ticker feature rows, and either explicit panel scores, a declared linear
scorer, or a local XGBoost artifact payload. `feature_frame` is treated as
already in scorer space. `raw_feature_frame` and source-space overrides must
carry artifact normalization metadata (`feature_means`, `feature_stds`,
`feature_norm_kind`) so live/sim/backtest use the same feature-space transform.
Missing metadata, missing features, missing scores, failed model admission, or
missing order quantities block buys and record `blocked_by`.

Order intents are not allowed to leave this repo unexplained. Use
`stamp_order_attribution()` or `EmitAttributedOrderIntentsTask`; every intent
must include ticker, action, quantity, source job/task, acceptance reason,
model type, sector, score snapshot, artifact fingerprint, and decision inputs.

## Pipeline Rule

All runtime flows are built from `renquant-common` Task/Job/Pipeline
primitives.

## Initial Split Source

`hallovorld/RenQuant` commit
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

## Local Test

```bash
PYTHONPATH=../renquant-common/src:src python -m pytest -q
```
