# renquant-pipeline

Runtime decision-pipeline repository for RenQuant.

This repo owns inference/runtime composition: preflight, regime gates,
candidate scoring, ranking, selection, rotation, QP, order-intent generation,
and decision-trace persistence. It consumes model artifacts through contracts;
it does not train models and does not submit broker orders.

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
