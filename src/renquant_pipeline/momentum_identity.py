"""Dependency-light momentum identity contract (stdlib only).

RQ#574 round 3: the umbrella's config-artifact-path gate must validate a
ledger component's ``expected_config_fingerprint`` in the pinned-path CI
environment, which deliberately installs NO heavy runtime deps — importing
the recipe from ``kernel.panel_pipeline.momentum_residual_scorer``
transitively required pandas. This module is the PUBLIC, import-light home of
the recipe; the scorer imports from here, so there is exactly one source.

The recipe (unchanged bytes-for-bytes from the scorer's private
implementation): ``momentum-<params_version>-<sha256(canonical params)[:16]>``
where canonical = ``json.dumps(dict(params), sort_keys=True,
separators=(",", ":"), allow_nan=False)``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

__all__ = ["params_fingerprint"]


def params_fingerprint(params: Mapping[str, Any]) -> str:
    """Deterministic training-config identity from the artifact's own params
    block. Recomputable from a published artifact by any reader — including
    environments with nothing but the standard library installed."""
    canon = json.dumps(dict(params), sort_keys=True, separators=(",", ":"),
                       allow_nan=False)
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    version = str(params.get("params_version", "unversioned"))
    return f"momentum-{version}-{digest}"
