"""The buy-floor unit guard must exist in BOTH VetoWeakBuysTask twins.

Measured 2026-07-29, while checking whether the pipeline pin could be
advanced past #219/#220: the unit guard added by #219 landed in the kernel
implementation only. The top-level public export resolved to the OTHER one:

    renquant_pipeline.VetoWeakBuysTask -> renquant_pipeline.panel_scoring
    kernel twin                        -> ...kernel.panel_pipeline.job_panel_scoring
    same object? False
    top-level carries the #219 guard?  False
    kernel twin carries the #219 guard? True

So the documented public symbol was the one WITHOUT the safety guard. The
twin's own docstring claims it is kept "in LOCKSTEP with the kernel twin",
which is exactly the kind of promise that needs a test behind it — this is
the same both-copies defect class as the disposed-lot tax-netting bug.
"""
from __future__ import annotations

import inspect

import pytest

import renquant_pipeline as rp
from renquant_pipeline import panel_scoring as twin
from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as kern


def test_the_domain_constants_agree():
    # Duplicated values (see the twin's module comment) — pinned, not trusted.
    assert twin.RANK_SCORE_DOMAIN_RAW == kern.RANK_SCORE_DOMAIN_RAW
    assert (twin.RANK_SCORE_DOMAIN_PROBABILITY
            == kern.RANK_SCORE_DOMAIN_PROBABILITY)
    assert twin.RANK_SCORE_DOMAIN_RAW != twin.RANK_SCORE_DOMAIN_PROBABILITY


def test_both_veto_twins_carry_the_unit_guard():
    for impl in (twin.VetoWeakBuysTask, kern.VetoWeakBuysTask):
        src = inspect.getsource(impl)
        assert "_rank_score_domain" in src, (
            f"{impl.__module__}.VetoWeakBuysTask compares a probability-domain "
            f"buy floor without checking the score domain first"
        )


def test_the_public_export_is_guarded():
    """The specific hole found: the top-level export was the unguarded copy."""
    assert "_rank_score_domain" in inspect.getsource(rp.VetoWeakBuysTask)


class _Ctx:
    """Minimal context for the twin's task protocol."""

    def __init__(self, scores, domain=None):
        self.panel_scores = dict(scores)
        self.scores = dict(scores)
        self.blocked_by = {}
        self.strategy_config = {
            "watchlist": list(scores),
            "ranking": {"panel_scoring": {"buy_floor": 0.20}},
        }
        if domain is not None:
            self._rank_score_domain = domain


# The PatchTST scale from the incident: all-negative raw output that can
# never clear a 0.20 probability floor.
RAW_SCORES = {"AAA": -0.11, "BBB": -0.05, "CCC": -0.30}


def test_raw_domain_refuses_instead_of_vetoing_the_cross_section():
    ctx = _Ctx(RAW_SCORES, domain=twin.RANK_SCORE_DOMAIN_RAW)
    assert twin.VetoWeakBuysTask().run(ctx) is False
    # Every name is blocked for the RIGHT reason — a unit refusal, not 3
    # independent "this score is weak" verdicts.
    assert set(ctx.blocked_by.values()) == {"rank_score_domain_uncalibrated"}
    assert not getattr(ctx, "accepted_candidates", [])


def test_probability_domain_is_compared_normally():
    ctx = _Ctx({"AAA": 0.55, "BBB": 0.05},
               domain=twin.RANK_SCORE_DOMAIN_PROBABILITY)
    assert twin.VetoWeakBuysTask().run(ctx) is True
    assert [c["ticker"] for c in ctx.accepted_candidates] == ["AAA"]
    assert ctx.blocked_by["BBB"] == "panel_score_below_buy_floor"


def test_absent_domain_keeps_the_previous_behaviour():
    """Callers that never scored through a model are not newly failed."""
    ctx = _Ctx({"AAA": 0.55, "BBB": 0.05})
    assert twin.VetoWeakBuysTask().run(ctx) is True
    assert [c["ticker"] for c in ctx.accepted_candidates] == ["AAA"]


@pytest.mark.parametrize("stage_name", ["ApplyScoresTask",
                                        "ApplyGlobalCalibrationTask"])
def test_both_twins_stamp_the_domain_somewhere_in_the_producing_stages(stage_name):
    """A guard nothing ever stamps for is inert — pin the producers too."""
    for mod in (twin, kern):
        src = inspect.getsource(getattr(mod, stage_name))
        assert "_rank_score_domain" in src, (
            f"{mod.__name__}.{stage_name} never records the score domain, so "
            f"the buy-floor guard can never fire"
        )
