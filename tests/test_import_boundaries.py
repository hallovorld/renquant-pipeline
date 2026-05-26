from __future__ import annotations

import importlib
import sys


def test_pipeline_import_does_not_pull_training_or_execution() -> None:
    importlib.import_module("renquant_pipeline")

    forbidden_prefixes = (
        "alpaca",
        "ib_insync",
        "live",
        "renquant_execution",
        "renquant_model_gbdt",
        "renquant_model_patchtst",
        "torch",
        "xgboost",
    )
    offenders = sorted(
        name for name in sys.modules
        if name in forbidden_prefixes or name.startswith(forbidden_prefixes)
    )
    assert offenders == []
