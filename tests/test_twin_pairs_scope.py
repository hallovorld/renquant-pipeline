"""GOAL-3 — the twin scanner's own scope must not be narrower than the defect.

`tools/twin_pairs.py` pins which copy of each documented symbol executes. It used to
select `n.endswith("Task")`, and `kernel_twin` matched `^class NAME` only. Two
enumerated scopes stacked: the first excluded names, the second excluded kinds. Both
pass forever for whatever falls outside them.

Measured 2026-07-31: 51 public exports, **19** with a same-named definition under
`kernel/`. The old scope covered **6**. The 13 it silently excluded include
`stamp_order_attribution`, `validate_order_attribution` and `score_snapshot` --
order-attribution on the capital path -- plus `PanelScoringJob` and `SelectionJob`,
Jobs that could never match a suffix looking for Tasks.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "twin_pairs.py"
PINS = ROOT / "twin_pairs.json"


def _load():
    spec = importlib.util.spec_from_file_location("twin_pairs_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


T = _load()

NEWLY_COVERED = {
    "PanelScoringJob", "SelectionJob", "active_scorer_identity",
    "build_ticker_daily_state_rows", "live_state_legacy_path", "live_state_path",
    "model_type_from_artifact", "resolve_live_state_read", "runs_db_legacy_path",
    "runs_db_path", "score_snapshot", "stamp_order_attribution",
    "validate_order_attribution",
}


def test_the_scan_applies_NO_name_filter():
    """Anti-regression on the exact defect. If a filter comes back, this fails."""
    import renquant_pipeline as rp

    assert sorted(T.public_export_names(rp)) == sorted(rp.__all__)

    # AST, not grep: the string `endswith("Task")` legitimately appears in the
    # docstring that EXPLAINS the removed filter. Grepping source text for a
    # construct that also occurs in prose is the line-oriented-regex mistake --
    # the measurement has to read the structure, not the characters.
    import ast

    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "endswith"]
    assert calls == [], [ast.dump(c) for c in calls]


def test_kernel_twin_finds_FUNCTIONS_not_only_classes():
    """The second scope. `stamp_order_attribution` is a def; it was invisible."""
    assert T.kernel_twin("stamp_order_attribution") is not None
    assert T.kernel_twin("PanelScoringJob") is not None          # class still works
    assert T.kernel_twin("NoSuchSymbolAnywhere") is None         # control


def test_every_documented_export_appears_in_the_pin_file():
    """The invariant that lets a reader trust the ABSENCE of a warning. Names that
    are not source objects are RECORDED, never dropped."""
    import renquant_pipeline as rp

    pinned = json.loads(PINS.read_text(encoding="utf-8"))["pairs"]
    assert set(pinned) == set(rp.__all__)
    unsourceable = {k for k, v in pinned.items() if v.get("kind")}
    assert unsourceable == {
        "ATTRIBUTION_VERSION", "DEFAULT_MAX_STALENESS_MINUTES",
        # pipeline#250 rollout step 2: string constants of .serving_features
        "SERVING_FEATURES_BLOCK_KEY", "SERVING_FEATURES_FILENAME",
    }


def test_the_thirteen_previously_invisible_twins_are_now_pinned():
    pinned = json.loads(PINS.read_text(encoding="utf-8"))["pairs"]
    for name in NEWLY_COVERED:
        assert name in pinned, name
        assert pinned[name].get("kernel_twin_file"), name


def test_the_capital_path_functions_are_among_them():
    """Named separately because these are the ones that matter most: order
    attribution is what stamps which model a live order came from."""
    pinned = json.loads(PINS.read_text(encoding="utf-8"))["pairs"]
    for name in ("stamp_order_attribution", "validate_order_attribution",
                 "score_snapshot"):
        assert "order_attribution" in pinned[name]["kernel_twin_file"]
        assert pinned[name]["public_is_kernel"] is False      # the twin still runs


def test_the_committed_pins_currently_hold():
    """The check must PASS on what is committed, or it is noise from day one."""
    problems = T.verify(json.loads(PINS.read_text(encoding="utf-8")))
    assert problems == [], problems


def test_the_twin_count_is_measured_not_asserted():
    pinned = json.loads(PINS.read_text(encoding="utf-8"))["pairs"]
    twins = [k for k, v in pinned.items() if v.get("kernel_twin_file")]
    # 51 -> 56 with pipeline#250 rollout step 2 (5 .serving_features exports,
    # none kernel-twinned)
    assert len(pinned) == 56
    assert len(twins) == 19
