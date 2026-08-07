"""A blocked rotation must be COUNTABLE, not just log-visible.

The defect these cover, measured 2026-08-06 on the production lane: both of the
day's runs logged

    kernel.pipeline.rotation: ROTATION_REJECT  swap=NVDA→CRWD  reason=correlation_guard
    kernel.pipeline:          InferencePipeline DONE  rotations_emitted=0 (considered=0  blocked=0)

Across 23 sessions with rotation evaluation the tree selected 4 swaps, all 4 were
rejected by a guard, and the summary read zero every time. The rejection existed
only as INFO-level prose; the one number a health check or digest would read said
"rotation had nothing to do" on exactly the runs where it tried and was stopped.

Two causes, both covered here:
  * every guard rejection in ValidatePairsTask was `log.info(...)` + `continue`,
    recording nothing on ctx;
  * `ctx.rotations` is overwritten with the survivors, so the summary's
    `len(ctx.rotations)` was the survivor count printed under the label
    "considered".
"""
from __future__ import annotations

import types

import pytest

from renquant_pipeline.kernel.pipeline.task_rotation import record_rotation_block


class _Pair:
    def __init__(self, sell, buy):
        self.sell_ticker = sell
        self.buy_ticker = buy


def _ctx():
    return types.SimpleNamespace()


# ── the recorder ───────────────────────────────────────────────────────────

def test_records_onto_a_context_with_no_list_yet():
    ctx = _ctx()
    record_rotation_block(ctx, _Pair("NVDA", "CRWD"), "correlation_guard")
    assert ctx.rotations_blocked == [
        {"sell": "NVDA", "buy": "CRWD", "reason": "correlation_guard"}
    ]


def test_appends_rather_than_replacing():
    # EmitRotationsTask writes into the same list; a recorder that reset it
    # would erase the suppression records that already exist.
    ctx = _ctx()
    ctx.rotations_blocked = [{"sell": "A", "buy": "B", "reason": "skip_buys"}]
    record_rotation_block(ctx, _Pair("NVDA", "CRWD"), "sector_cap")
    assert len(ctx.rotations_blocked) == 2
    assert ctx.rotations_blocked[0]["reason"] == "skip_buys"


def test_shape_matches_the_emit_task_record():
    # kernel/persistence.py reads this list; a different key set would be
    # dropped silently rather than raising.
    ctx = _ctx()
    record_rotation_block(ctx, _Pair("NVDA", "CRWD"), "wash_sale")
    assert set(ctx.rotations_blocked[0]) == {"sell", "buy", "reason"}


@pytest.mark.parametrize("reason", ["wash_sale", "sector_cap", "correlation_guard"])
def test_every_validate_guard_reason_round_trips(reason):
    ctx = _ctx()
    record_rotation_block(ctx, _Pair("X", "Y"), reason)
    assert ctx.rotations_blocked[0]["reason"] == reason


# ── every rejection site is wired ──────────────────────────────────────────

def test_no_bare_continue_survives_in_validate_pairs():
    """Every ROTATION_REJECT inside ValidatePairsTask must record before it
    continues. A new guard added later with a bare log+continue re-opens the
    exact hole this fix closes, and nothing else would catch it."""
    import inspect

    from renquant_pipeline.kernel.pipeline import task_rotation

    src = inspect.getsource(task_rotation.ValidatePairsTask)
    n_rejects = src.count("ROTATION_REJECT")
    n_records = src.count("record_rotation_block(")
    assert n_rejects > 0, "guard against the test silently matching nothing"
    assert n_records == n_rejects, (
        f"{n_rejects} ROTATION_REJECT site(s) but {n_records} record call(s) — "
        "a rejection that records nothing is invisible to the run counters"
    )


def test_considered_is_preserved_before_the_overwrite():
    import inspect

    from renquant_pipeline.kernel.pipeline import task_rotation

    src = inspect.getsource(task_rotation.ValidatePairsTask)
    assert "ctx.rotations_considered" in src
    # The preservation must happen BEFORE ctx.rotations is replaced, or it
    # records the survivor count again under a new name.
    assert src.index("ctx.rotations_considered") < src.index("ctx.rotations = validated")


# ── the summary line ───────────────────────────────────────────────────────

def _summary_counts(ctx):
    """Mirror of the counter block in pp_inference.run()."""
    n_considered = int(
        getattr(ctx, "rotations_considered", None)
        if getattr(ctx, "rotations_considered", None) is not None
        else len(ctx.rotations)
    )
    n_emitted = int(ctx.counters.get("rotations", 0))
    n_blocked = len(getattr(ctx, "rotations_blocked", []) or [])
    return n_considered, n_emitted, n_blocked


def test_the_2026_08_06_run_would_now_report_one_and_one():
    """The exact live shape: one pair considered, rejected by correlation_guard,
    none emitted. Pre-fix this printed (considered=0 blocked=0)."""
    ctx = _ctx()
    ctx.counters = {}
    ctx.rotations = []                 # survivors, post-overwrite
    ctx.rotations_considered = 1
    record_rotation_block(ctx, _Pair("NVDA", "CRWD"), "correlation_guard")
    assert _summary_counts(ctx) == (1, 0, 1)


def test_a_clean_run_still_reports_all_zero():
    ctx = _ctx()
    ctx.counters = {}
    ctx.rotations = []
    ctx.rotations_considered = 0
    assert _summary_counts(ctx) == (0, 0, 0)


def test_emitted_and_blocked_coexist():
    ctx = _ctx()
    ctx.counters = {"rotations": 1}
    ctx.rotations = [_Pair("A", "B")]
    ctx.rotations_considered = 2
    record_rotation_block(ctx, _Pair("NVDA", "CRWD"), "sector_cap")
    assert _summary_counts(ctx) == (2, 1, 1)


def test_context_that_never_reached_validate_falls_back():
    # SellOnlyPipeline and early-return paths never set rotations_considered;
    # the old expression must still work rather than raising.
    ctx = _ctx()
    ctx.counters = {}
    ctx.rotations = [_Pair("A", "B")]
    assert _summary_counts(ctx) == (1, 0, 0)


def test_zero_considered_is_not_confused_with_absent():
    # 0 is a real measurement; the fallback must not fire on it and substitute
    # the survivor count.
    ctx = _ctx()
    ctx.counters = {}
    ctx.rotations = [_Pair("A", "B")]   # would give 1 under the fallback
    ctx.rotations_considered = 0
    assert _summary_counts(ctx)[0] == 0
