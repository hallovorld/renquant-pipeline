"""Shadow of renquant-common's net_safety — delegates to the canonical module.

Lifted to renquant-common (PR #7 in that repo, 2026-06-01). This file was
the original umbrella shadow before the lift. Now a pure re-export so any
renquant_pipeline.kernel.net_safety consumer transparently uses the
single-source-of-truth implementation.

Once consumers are switched directly to renquant_common.net_safety (see
RenQuant umbrella task #25/#26 follow-ups), this file can be deleted.
"""
from renquant_common.net_safety import FetchBudget, call_with_timeout  # noqa: F401

__all__ = ["FetchBudget", "call_with_timeout"]
