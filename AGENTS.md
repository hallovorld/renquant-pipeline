# Repository Guidelines

## Project Structure & Module Organization

This is a Python `src/` layout. Runtime code lives in
`src/renquant_pipeline/`; tests live in `tests/`; repo context lives in
`README.md`, `RENQUANT_REPOS.md`, `renquant_repo.yml`, and `docs/`.

Important areas:

- `kernel/pipeline/`: runtime gates, ranking, selection, exits, order intents,
  and decision traces.
- `kernel/preflight_pipeline/`: artifact, config, data, and broker readiness.
- `kernel/panel_pipeline/`: feature matrix, scorer loading, panel admission,
  and weak-buy vetoes.
- `kernel/portfolio_qp/`: QP sizing, signal combination, and joint actions.
- `kernel/execution/`: simulation-facing execution types, fees, slippage, and
  settlement. Live broker submission belongs in `renquant-execution`.

## Build, Test, and Development Commands

- `make test`: runs pytest with the repo `PYTHONPATH` configured.
- `make doctor`: imports the public pipeline surface and shared primitives.
- `python -m pytest -q tests/test_selection_contract.py`: runs one focused test
  file during iteration.

The Makefile prefers `.venv/bin/python`, then `python3`. Local imports expect
sibling repos such as `../renquant-common`, `../renquant-base-data`, and
`../renquant-artifacts`.

## Coding Style & Naming Conventions

Target Python 3.10+. Use four-space indentation, typed public interfaces, and
dataclasses where they clarify contracts. Keep module names lowercase with
underscores. Tests use `test_*.py`; task modules use `task_*.py`; job modules
use `job_*.py`.

Use `renquant-common` Task, Job, and Pipeline primitives. Do not add hidden
fallbacks for missing scores, stale metadata, or absent fingerprints; fail
closed and preserve the reason in decision traces.

## Testing Guidelines

Pytest is the test runner. Add focused tests beside related coverage in
`tests/`. Boundary tests guard forbidden imports, bare `kernel.*` imports, and
artifact-contract shims; satisfy those guards rather than weakening them. When
changing scorer contracts, QP, selection, preflight, or traces, run `make test`.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style subjects such as
`feat(preflight): ...`, `fix(sim): ...`, and `chore(phase5): ...`. Keep subjects
imperative, scoped, and concise.

PRs should describe behavior, list tests run, and call out contract impact. Use
feature branches, rebase on `origin/main`, and do not push directly to `main`.
After merge, the umbrella repo must advance its pin before production use.

## Cross-Repo Runtime Boundaries

This repo consumes strategy config, data manifests, and artifact manifests. It
exports decision traces, gate reports, and order intents. Upstream changes in
`renquant-common`, `renquant-base-data`, `renquant-artifacts`, or
`renquant-model` can break admission. Downstream `renquant-execution`,
`renquant-backtesting`, and `renquant-orchestrator` rely on stable exports from
`renquant_pipeline.__init__`, order-attribution fields, and trace schemas.

Do not add model training, broker order submission, raw data storage, secrets,
large artifacts, generated caches, or environment-specific paths.
