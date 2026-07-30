"""An unrecognised scoring kind is a provenance gap, not a pass.

The module's own 2026-06-27 note records that this check once skipped for every
non-`hf_patchtst` kind, so "the staleness rail did nothing for the model actually
driving trades". That was fixed by ADDING `xgb` to an allow-list — which leaves the
default fail-OPEN, and the same shape recurred.

Measured 2026-07-30 on the live machine: three lanes ran, and the check behaved
differently on each.

  prod   -> `effective_train_cutoff_date` unstamped, SURFACED as a gap
  shadow -> `train-cutoff age 624d`,               SURFACED
  blend  -> `kind='blend' unrecognized — staleness skip`, NOT CHECKED AT ALL

The blend lane was issuing buy recommendations at the time. `patchtst` (no `hf_`
prefix) and an absent kind (`None`) also appear in committed strategy configs and
took the same branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from renquant_pipeline.kernel.preflight_pipeline.tasks.staleness import (
    ModelStalenessTask,
)


class _Ctx:
    def __init__(self, strategy_dir: Path, kind, rel="art.json"):
        self.strategy_dir = strategy_dir
        self.config = {"ranking": {"panel_scoring": {
            "enabled": True, "kind": kind, "artifact_path": rel}}}


def _artifact(tmp_path: Path, payload: dict, name="art.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return p


@pytest.mark.parametrize("kind", ["blend", "patchtst", "ensemble", "", None])
def test_an_unrecognised_kind_without_trained_date_FAILS(tmp_path, kind):
    """THE DEFECT. Every one of these used to return passed=True."""
    _artifact(tmp_path, {"note": "no dates here"})
    res = ModelStalenessTask().check(_Ctx(tmp_path, kind))
    assert res.ok is False, f"kind={kind!r} came back as a pass (res.ok)"
    assert "NOT a pass" in res.message


def test_an_unrecognised_kind_WITH_a_trained_date_is_still_measured(tmp_path):
    """Best-effort, not blanket refusal: a kind that happens to stamp its date on
    the artifact JSON gets measured rather than dismissed. Refusing everything
    unknown would trade a fail-open for a permanent alarm."""
    # BOTH dates: the module's existing contract treats an absent
    # effective_train_cutoff_date as its own provenance gap ("SURFACE, not skip"),
    # so trained_date alone is not a pass for ANY kind. My first fixture omitted it
    # and the test failed on my expectation, not on the fix.
    _artifact(tmp_path, {"trained_date": "2026-07-29",
                         "effective_train_cutoff_date": "2026-07-20"})
    res = ModelStalenessTask().check(_Ctx(tmp_path, "blend"))
    assert res.ok is True, res.message


def test_an_unrecognised_kind_with_an_OLD_trained_date_still_alarms(tmp_path):
    """Anti-vacuity for the branch above: measuring must be able to FAIL, or the
    best-effort read is just the old skip wearing a new message."""
    _artifact(tmp_path, {"trained_date": "2020-01-02"})
    res = ModelStalenessTask().check(_Ctx(tmp_path, "blend"))
    assert res.ok is False, res.message


def test_an_unreadable_artifact_under_an_unknown_kind_FAILS(tmp_path):
    (tmp_path / "art.json").write_text("{not json")
    res = ModelStalenessTask().check(_Ctx(tmp_path, "blend"))
    assert res.ok is False
    assert "unmeasurable" in res.message


@pytest.mark.parametrize("kind", ["xgb", "panel_ltr_xgboost"])
def test_the_RECOGNISED_kinds_are_unchanged(tmp_path, kind):
    """Anti-regression: inverting the default must not perturb the paths that
    already worked."""
    _artifact(tmp_path, {"trained_date": "2026-07-29",
                         "effective_train_cutoff_date": "2026-07-20"})
    assert ModelStalenessTask().check(_Ctx(tmp_path, kind)).ok is True


def test_panel_scoring_disabled_still_skips(tmp_path):
    """The one legitimate skip. If this broke, every non-panel strategy would
    alarm forever and the check would be turned off wholesale."""
    ctx = _Ctx(tmp_path, "blend")
    ctx.config["ranking"]["panel_scoring"]["enabled"] = False
    assert ModelStalenessTask().check(ctx).ok is True
