"""Meta-labeling primitives (López de Prado, AFML ch.20).

Only the pure labeling primitives with no model/scorer dependencies live
here. ``SnapshotLogger`` / ``FEATURE_COLUMNS`` (the umbrella sibling at
``backtesting/renquant_104/kernel/meta_label/__init__.py``) is decision-
pipeline-adjacent but ties into ``ctx.metadata`` and ``exit_types`` —
kept in umbrella / renquant-backtesting until those deps are also lifted.

Lift scope here: ``triple_barrier`` only — pure stdlib + pandas/numpy,
zero ``kernel.*`` imports inside the module. Adding more requires re-running
the import-boundary audit.
"""
from .triple_barrier import apply_triple_barrier, meta_label_for_exit_event

__all__ = ["apply_triple_barrier", "meta_label_for_exit_event"]
