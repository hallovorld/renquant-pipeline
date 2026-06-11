"""Root-cause fix: signal-direction gate on calibrated μ for an all-negative ranker.

2026-06-10 finding (verified on real data, 142 names asof 2026-06-10): the prod
PatchTST raw panel_score is intrinsically all-negative (output head centres near
raw≈−0.20; signal is in the relative ordering, OOS IC≈0.13). The calibrator's
ER=0 neutral sits at raw≈−0.198, so a positive calibrated μ means "ranked above
the model's neutral". The legacy raw>0 gate blocks 100% of names (0/142) →
structural sell-only; the calibrated μ>0 gate admits the bullish names (80/142).

``signal_gate_prefer_calibrated_mu`` (default OFF) switches the direction test
to μ>0 alone when a calibrated μ is present. These tests pin both modes.
"""
from __future__ import annotations

from renquant_pipeline.kernel.pipeline.signal_direction import (
    REASON_NEGATIVE_RAW,
    REASON_NONPOSITIVE_ER,
    long_signal_ok,
)

# Panel scoring enabled; default legacy gate (raw>0 AND μ>0).
_CFG_LEGACY = {"ranking": {"panel_scoring": {"enabled": True}}}
# The fix: prefer calibrated μ as the direction test.
_CFG_MU = {"ranking": {"panel_scoring": {
    "enabled": True, "signal_gate_prefer_calibrated_mu": True}}}


def test_legacy_blocks_negative_raw_even_with_positive_mu() -> None:
    """Default OFF: a top-ranked PatchTST name (raw<0, μ>0) is still blocked —
    the structural sell-only behaviour, preserved byte-for-byte."""
    ok, reason = long_signal_ok(-0.073, _CFG_LEGACY, expected_return=+0.059)
    assert not ok and reason == REASON_NEGATIVE_RAW


def test_mu_mode_admits_top_ranked_negative_raw_name() -> None:
    """The fix: with μ-primary on, the same DDOG-like name (raw=−0.073,
    μ=+0.059) is admitted — μ>0 ⟺ raw>neutral_raw."""
    ok, reason = long_signal_ok(-0.073, _CFG_MU, expected_return=+0.059)
    assert ok and reason == ""


def test_mu_mode_still_blocks_bottom_ranked_name() -> None:
    """A bottom-ranked name (raw far negative, μ≤0) is still refused — we long
    only names ranked ABOVE the model's neutral, not every negative name."""
    ok, reason = long_signal_ok(-0.26, _CFG_MU, expected_return=-0.03)
    assert not ok and reason == REASON_NONPOSITIVE_ER


def test_mu_mode_does_not_apply_raw_conjunct() -> None:
    """In μ-primary mode the raw>0 conjunct must NOT fire (that is the whole
    point) — a positive μ admits regardless of the negative raw sign."""
    ok, _ = long_signal_ok(-0.20, _CFG_MU, expected_return=+0.001)
    assert ok


def test_mu_mode_falls_back_to_raw_when_mu_absent() -> None:
    """No calibrated μ (calibrator off) → there must still be a direction test;
    fall back to the legacy raw>0 gate."""
    ok, reason = long_signal_ok(-0.05, _CFG_MU, expected_return=None)
    assert not ok and reason == REASON_NEGATIVE_RAW
    ok2, _ = long_signal_ok(+0.05, _CFG_MU, expected_return=None)
    assert ok2


def test_mu_mode_inert_when_panel_scoring_disabled() -> None:
    cfg = {"ranking": {"panel_scoring": {
        "enabled": False, "signal_gate_prefer_calibrated_mu": True}}}
    ok, _ = long_signal_ok(-0.5, cfg, expected_return=-0.5)
    assert ok


def test_full_universe_admission_counts_match_real_data() -> None:
    """Mirror the live verification: a small panel where raw is all-negative
    but μ crosses zero. Legacy admits 0; μ-mode admits the μ>0 names."""
    universe = [
        ("DDOG", -0.073, +0.059), ("SMCI", -0.073, +0.059),
        ("FTNT", -0.079, +0.057), ("QCOM", -0.090, +0.051),
        ("MIDB", -0.193, +0.002), ("LOWB", -0.205, -0.001),
        ("WRST", -0.268, -0.034),
    ]
    legacy = sum(long_signal_ok(r, _CFG_LEGACY, expected_return=m)[0]
                 for _, r, m in universe)
    mu = sum(long_signal_ok(r, _CFG_MU, expected_return=m)[0]
             for _, r, m in universe)
    assert legacy == 0                # raw>0 blocks every (negative) name
    assert mu == sum(1 for _, _, m in universe if m > 0)  # the μ>0 names
