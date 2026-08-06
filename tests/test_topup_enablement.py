"""Top-up must not live or die with Kelly.

Top-up is the ONLY buy path that can put cash into a position the book already
holds; every other path needs a free slot. Its only gate was
``ranking.kelly_sizing.enabled``.

Measured 2026-08-06 on the live book: Kelly is `enabled=false`, switched off on
2026-08-04 for an unrelated reason the config states itself — `use_calibrator_mu`
wires Kelly's mu from the calibrator, and the z-blend promotion turned
`global_calibration` off, so that INPUT ceased to exist. Top-up was collateral
damage: its own conviction test reads `rank_score`, which the blend produces
normally.

The cost is arithmetic: `max_position_pct` 0.12 x `confidence_to_size_multiplier`
0.57 = 6.84% per position, x 8 slots = **54.7% maximum deployment** — a hard ~45%
cash floor. The book sat at 46.6% invested / 53.4% cash with the one lever that
could raise EXISTING weights switched off by a flag that was never about it.

These tests pin the decoupling AND its backward compatibility: no config in the
tree carries a `ranking.top_up` section, so every existing lane must resolve
exactly as before.
"""
from __future__ import annotations

import pytest

from renquant_pipeline.kernel.pipeline.task_topup import resolve_topup_enablement


# ── backward compatibility: nothing changes until a config opts in ─────────

def test_no_topup_section_falls_back_to_kelly_off():
    enabled, _, src = resolve_topup_enablement(
        {"ranking": {"kelly_sizing": {"enabled": False}}})
    assert enabled is False
    assert "kelly_sizing.enabled" in src


def test_no_topup_section_falls_back_to_kelly_on():
    enabled, _, src = resolve_topup_enablement(
        {"ranking": {"kelly_sizing": {"enabled": True}}})
    assert enabled is True
    assert "kelly_sizing.enabled" in src


def test_the_legacy_source_string_names_the_coupling():
    """A future reader must not have to guess which section decided."""
    _, _, src = resolve_topup_enablement({"ranking": {"kelly_sizing": {}}})
    assert "legacy coupling" in src


@pytest.mark.parametrize("cfg", [None, {}, {"ranking": None},
                                 {"ranking": {}}, {"ranking": {"kelly_sizing": None}}])
def test_absent_or_broken_config_is_off_not_a_crash(cfg):
    enabled, knobs, src = resolve_topup_enablement(cfg)
    assert enabled is False
    assert isinstance(knobs, dict) and isinstance(src, str)


# ── the decoupling ─────────────────────────────────────────────────────────

def test_topup_can_run_while_kelly_is_off():
    """The whole point: Kelly off for its own reason, top-up on for its own."""
    enabled, _, src = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": False},
        "top_up": {"enabled": True},
    }})
    assert enabled is True
    assert src == "ranking.top_up.enabled"


def test_topup_can_be_off_while_kelly_is_on():
    # The reverse direction must work too, or the new flag is only an override
    # in one direction and the coupling survives half-intact.
    enabled, _, src = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": True},
        "top_up": {"enabled": False},
    }})
    assert enabled is False
    assert src == "ranking.top_up.enabled"


def test_presence_of_the_key_decides_not_its_truthiness():
    """`enabled: false` is a DECISION, not an absent section. Resolving on
    truthiness would silently fall back to Kelly and re-couple them."""
    _, _, src = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": True},
        "top_up": {"enabled": False},
    }})
    assert src == "ranking.top_up.enabled"


def test_empty_topup_section_is_not_an_opt_in():
    # A section added for its knobs but with no `enabled` must not silently
    # flip the switch either way.
    enabled, _, src = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": True},
        "top_up": {"top_up_threshold": 0.03},
    }})
    assert enabled is True
    assert "kelly_sizing.enabled" in src


# ── knob resolution ────────────────────────────────────────────────────────

def test_topup_knobs_override_kelly_knobs():
    _, knobs, _ = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": False, "top_up_threshold": 0.05,
                         "topup_conviction_floor": 0.55},
        "top_up": {"enabled": True, "top_up_threshold": 0.02},
    }})
    assert knobs["top_up_threshold"] == 0.02        # overridden
    assert knobs["topup_conviction_floor"] == 0.55  # inherited


def test_kelly_knobs_are_inherited_so_a_minimal_section_works():
    """An operator enabling top-up must not have to restate every threshold."""
    _, knobs, _ = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": False, "top_up_threshold": 0.05,
                         "per_session_buy_cap": 3},
        "top_up": {"enabled": True},
    }})
    assert knobs["top_up_threshold"] == 0.05
    assert knobs["per_session_buy_cap"] == 3


def test_provenance_comment_keys_are_not_treated_as_knobs():
    # The configs carry `_reason` / `_note` keys everywhere; leaking them into
    # the knob dict would let a comment shadow a real setting.
    _, knobs, _ = resolve_topup_enablement({"ranking": {
        "kelly_sizing": {"enabled": False, "top_up_threshold": 0.05},
        "top_up": {"enabled": True, "_reason": "operator directive"},
    }})
    assert "_reason" not in knobs
    assert knobs["top_up_threshold"] == 0.05


# ── the live config, as it stands today ────────────────────────────────────

def test_the_live_config_shape_still_resolves_off():
    """Reproduces the pinned 2026-08-06 shape: Kelly off, no top_up section.
    This change must NOT turn top-up on anywhere by itself — enabling it is a
    separate decision with its own evidence."""
    live = {"ranking": {"kelly_sizing": {
        "enabled": False, "top_up_threshold": 0.05,
        "topup_conviction_floor": 0.55, "use_calibrator_mu": True,
    }}}
    enabled, knobs, src = resolve_topup_enablement(live)
    assert enabled is False
    assert knobs["top_up_threshold"] == 0.05
    assert "kelly_sizing.enabled" in src


def test_task_reads_through_the_resolver_not_kelly_directly():
    """A later edit that reaches back into `kelly_sizing` for the enable check
    re-creates the coupling silently."""
    import inspect

    from renquant_pipeline.kernel.pipeline.task_topup import TopUpHeldTask

    src = inspect.getsource(TopUpHeldTask.run)
    assert "resolve_topup_enablement" in src
    assert 'kelly_cfg.get("enabled"' not in src
