"""Public re-export surface for cross-repo consumers.

V-005 remediation: orchestrator (and potentially other sibling repos) need
a handful of types and functions from kernel internals.  Rather than
importing from ``renquant_pipeline.kernel.*`` directly (fragile coupling
to internal module layout), consumers import from this stable surface.

Add symbols here only when a sibling repo has a demonstrated need.
"""
from __future__ import annotations

from renquant_pipeline.kernel.data import (
    LocalStore,
    _last_completed_nyse_session as last_completed_nyse_session,
)
from renquant_pipeline.kernel.exits import HoldingState
from renquant_pipeline.kernel.regime import RegimeState
from renquant_pipeline.kernel.pipeline.job_universe import (
    LoadUniverseJob,
    UniverseContext,
)
from renquant_pipeline.kernel.persistence import record_training_run

__all__ = [
    "HoldingState",
    "last_completed_nyse_session",
    "LoadUniverseJob",
    "LocalStore",
    "record_training_run",
    "RegimeState",
    "UniverseContext",
]
