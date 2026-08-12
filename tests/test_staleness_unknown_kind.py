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

2026-08-11: `blend` is now a REGISTERED scoring kind — its stalest-leg freshness
rail lives in ``ModelStalenessTask._check_blend`` and is exercised by
``test_staleness_blend.py``. So this module now uses ``ensemble`` (and
``patchtst`` / ``""`` / ``None``) as the still-unregistered representatives that
must fail closed via the inverted-default else branch.
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


@pytest.mark.parametrize("kind", ["patchtst", "ensemble", "", None])
def test_an_unrecognised_kind_without_trained_date_FAILS(tmp_path, kind):
    """THE DEFECT. Every one of these used to return passed=True."""
    _artifact(tmp_path, {"note": "no dates here"})
    res = ModelStalenessTask().check(_Ctx(tmp_path, kind))
    assert res.ok is False, f"kind={kind!r} came back as a pass (res.ok)"
    assert "not a registered scoring kind" in res.message
    assert "NOT a staleness pass" in res.message


def test_an_unrecognised_kind_with_FRESH_dates_STILL_DOES_NOT_PASS(tmp_path):
    """CORRECTED at review (#233). My first fix let an unknown kind PASS when its
    dates happened to be fresh. That silently CERTIFIES A NEW MODEL KIND: freshness
    being measurable does not establish that the artifact carries the schema or
    training provenance this rail requires. Never a pass — but the measured
    freshness IS reported, or the finding is unactionable."""
    # BOTH dates: the module's existing contract treats an absent
    # effective_train_cutoff_date as its own provenance gap ("SURFACE, not skip"),
    # so trained_date alone is not a pass for ANY kind. My first fixture omitted it
    # and the test failed on my expectation, not on the fix.
    _artifact(tmp_path, {"trained_date": "2026-07-29",
                         "effective_train_cutoff_date": "2026-07-20"})
    res = ModelStalenessTask().check(_Ctx(tmp_path, "ensemble"))
    assert res.ok is False, res.message
    assert "not a registered scoring kind" in res.message
    # the measurement must survive into the message
    assert "2026-07-29" in res.message and "2026-07-20" in res.message


def test_the_reported_measurement_distinguishes_fresh_from_stale(tmp_path):
    """Anti-vacuity for the message. Non-passing is now unconditional, so the ONLY
    thing that makes the finding actionable is whether the reader can tell a
    routine registration from an urgent one. If both cases printed the same text
    the measurement would be decorative."""
    _artifact(tmp_path, {"trained_date": "2020-01-02"})
    old = ModelStalenessTask().check(_Ctx(tmp_path, "ensemble")).message
    _artifact(tmp_path, {"trained_date": "2026-07-29"})
    new = ModelStalenessTask().check(_Ctx(tmp_path, "ensemble")).message
    assert old != new
    assert "2020-01-02" in old and "2026-07-29" in new


def test_an_unreadable_artifact_under_an_unknown_kind_FAILS(tmp_path):
    (tmp_path / "art.json").write_text("{not json")
    res = ModelStalenessTask().check(_Ctx(tmp_path, "ensemble"))
    assert res.ok is False
    assert "not a registered scoring kind" in res.message
    assert "unreadable" in res.message


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
    ctx = _Ctx(tmp_path, "ensemble")
    ctx.config["ranking"]["panel_scoring"]["enabled"] = False
    assert ModelStalenessTask().check(ctx).ok is True
