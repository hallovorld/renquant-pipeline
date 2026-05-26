# Source Map From Monorepo

Initial source commit:
`8f3e08d8d1ae1e402a78f4815efb59e3c7c66aa8`.

Runtime pipeline code should be ported in reviewed slices from:

- `backtesting/renquant_104/kernel/pipeline/`
- `backtesting/renquant_104/kernel/portfolio_qp/`
- `backtesting/renquant_104/kernel/preflight.py`
- `backtesting/renquant_104/kernel/model_acceptance.py`
- `backtesting/renquant_104/kernel/persistence.py`
- runtime scorer interfaces currently mixed into
  `backtesting/renquant_104/kernel/panel_pipeline/`

Do not port model training loops or live broker order submission into this
repo. The pipeline emits order intents and decision trace rows; execution owns
broker mutation.
