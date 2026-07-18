"""VetoWeakBuys small-n guard — stage 1 tests (RFC 2026-07-17, pipeline #204).

Covers RFC §3.2 (a)-(f) plus the design-review approval note:

(a) bit-identity at n >= N0 on the RECORDED 2026-07-10 n=85 live scores
    (fixture ``vetoweakbuys_20260710_recorded_scores.json``, decision-ledger
    provenance, evidence pack of orchestrator PR #543);
(b) n < N0 relax-only floor — the RECORDED 2026-07-16 / 2026-07-17 n=5
    all-veto sessions admit {ATI, EME, BWXT} and veto {XLI, XLY} under the
    proposed production config (min_n=12, abs=0.50) [RFC AC-a];
(c) BOTH half-config directions + out-of-bounds values (min_n=1, min_n=100,
    absolute=0, absolute=1.2, absolute=NaN) → loud ERROR rejection +
    bit-identical status-quo floor;
(d) NaN candidates excluded from the guard's finite-score n;
(e) all three adaptive modes' small-n branches;
(f) ONE-SIDEDNESS on the synthetic Platt-compressed sets (range 0.07
    centered 0.45 / 0.55) — admitted count with the guard >= status quo —
    plus the pathological pre-existing misconfig cap < buy_floor_min in
    ``adaptive_mean_std_cap``, where the unconditional relax-only clamp must
    degrade to EXACTLY the status-quo floor instead of raising it
    (approval-note hardening).

Also pins kernel/twin LOCKSTEP: the ``renquant_pipeline.panel_scoring`` twin
computes bit-identical floors for every mode/config/score-set combination.
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
from types import SimpleNamespace

import pytest

from renquant_pipeline.inference import InferenceContext
from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import (
    VetoWeakBuysTask,
)
from renquant_pipeline import panel_scoring as twin

FIXTURE = (
    pathlib.Path(__file__).parent
    / "fixtures"
    / "vetoweakbuys_20260710_recorded_scores.json"
)

# Proposed production config (RFC §2.1) — stage 1 ships the MECHANISM only;
# these values activate via a separate renquant-strategy-104 config PR.
GUARD = {"buy_floor_min_n": 12, "buy_floor_absolute_smalln": 0.50}

# Recorded 2026-07-16 governed-override session (n=5, all-vetoed live at
# floor=mean+1σ=0.561104…; 3dp: ATI .557 EME .548 BWXT .533 XLI .449 XLY .448).
S_0716 = [
    ("ATI", 0.557459136834569),
    ("EME", 0.5477081929836948),
    ("BWXT", 0.5329365823764792),
    ("XLI", 0.44931989852357557),
    ("XLY", 0.448368428443952),
]
FLOOR_0716 = 0.561104062882113

# Recorded 2026-07-17 governed-override session (n=5, all-vetoed live at
# floor=0.576500…; 3dp: BWXT .564 EME .559 ATI .558 XLI .449 XLY .448).
S_0717 = [
    ("BWXT", 0.56368464048377),
    ("EME", 0.5588641871099314),
    ("ATI", 0.5575464063449799),
    ("XLI", 0.44931989852357557),
    ("XLY", 0.448368428443952),
]
FLOOR_0717 = 0.5765004367114172

# Synthetic Platt-compressed cross-sections (RFC §3.2(f)): range 0.07,
# centered 0.45 and 0.55.
COMPRESSED_045 = [0.415, 0.430, 0.450, 0.470, 0.485]
COMPRESSED_055 = [0.515, 0.530, 0.550, 0.570, 0.585]

ALL_MODES = ("adaptive_mean_std", "adaptive_mean_std_cap", "adaptive_quantile")


def _pairs(scores):
    if scores and isinstance(scores[0], (tuple, list)):
        return [(str(t), s) for t, s in scores]
    return [(f"T{i:03d}", s) for i, s in enumerate(scores)]


def _kernel_ctx(scores, raw_floor="adaptive_mean_std", **panel_cfg):
    cands = [
        SimpleNamespace(ticker=t, rank_score=s) for t, s in _pairs(scores)
    ]
    cfg = {"buy_floor": raw_floor, **panel_cfg}
    return SimpleNamespace(
        candidates=cands,
        config={"ranking": {"panel_scoring": cfg}},
        counters={},
    )


def _run_kernel(scores, raw_floor="adaptive_mean_std", **panel_cfg):
    ctx = _kernel_ctx(scores, raw_floor=raw_floor, **panel_cfg)
    VetoWeakBuysTask().run(ctx)
    kept = [c.ticker for c in ctx.candidates]
    return ctx, ctx._panel_buy_floor, ctx._panel_buy_floor_label, kept


def _twin_ctx(scores, raw_floor="adaptive_mean_std", **panel_cfg):
    pairs = _pairs(scores)
    cfg = {"enabled": True, "buy_floor": raw_floor, **panel_cfg}
    ctx = InferenceContext(
        strategy_config={
            "watchlist": [t for t, _ in pairs],
            "ranking": {"panel_scoring": cfg},
        },
        data_manifest={},
        artifact_manifest={"kind": "panel_ltr_xgboost"},
        market_snapshot={},
    )
    setattr(ctx, "panel_scores", {t: s for t, s in pairs})
    return ctx


def _run_twin(scores, raw_floor="adaptive_mean_std", **panel_cfg):
    ctx = _twin_ctx(scores, raw_floor=raw_floor, **panel_cfg)
    twin.VetoWeakBuysTask().run(ctx)
    kept = [row["ticker"] for row in ctx.accepted_candidates]
    return ctx, ctx._panel_buy_floor, ctx._panel_buy_floor_label, kept


def _recorded_0710():
    payload = json.loads(FIXTURE.read_text())
    rows = [(str(t), float(s)) for t, s in payload["admitted"] + payload["vetoed"]]
    return rows, payload


# ────────────────────────────────────────────────────────────────────────────
# (a) bit-identity at n >= N0 — recorded 2026-07-10 n=85 live session
# ────────────────────────────────────────────────────────────────────────────


def test_recorded_0710_status_quo_reproduces_live_floor_exactly() -> None:
    """Anchor: the unguarded formula reproduces the RECORDED live floor
    bit-for-bit, so bit-identity below is measured against ground truth."""
    rows, payload = _recorded_0710()
    assert len(rows) == payload["n"] == 85
    _, floor, _, kept = _run_kernel(rows, buy_floor_min=0.20)
    assert floor == payload["recorded_floor"]
    assert sorted(kept) == sorted(t for t, _ in payload["admitted"])


@pytest.mark.parametrize("mode", ALL_MODES)
def test_guard_is_bit_identical_at_n85_all_modes(mode) -> None:
    """n=85 >= N0=12: guarded floor, label, and kept set are IDENTICAL to
    the unguarded run — the guard must not perturb normal-n behavior."""
    rows, _ = _recorded_0710()
    _, floor_base, label_base, kept_base = _run_kernel(
        rows, raw_floor=mode, buy_floor_min=0.20
    )
    _, floor_g, label_g, kept_g = _run_kernel(
        rows, raw_floor=mode, buy_floor_min=0.20, **GUARD
    )
    assert floor_g == floor_base          # bit-identical, not approx
    assert label_g == label_base          # no smalln-relax label at n >= N0
    assert kept_g == kept_base


def test_guard_boundary_n_equals_min_n_is_status_quo() -> None:
    """n == buy_floor_min_n is NOT small-n (strict `<` per RFC §2.1)."""
    scores = [0.52 + 0.005 * i for i in range(12)]  # n == 12
    _, floor_base, label_base, _ = _run_kernel(scores, buy_floor_min=0.20)
    _, floor_g, label_g, _ = _run_kernel(scores, buy_floor_min=0.20, **GUARD)
    assert floor_g == floor_base
    assert label_g == label_base


# ────────────────────────────────────────────────────────────────────────────
# (b) recorded 2026-07-16 / 2026-07-17 small-n sessions [RFC AC-a]
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scores,recorded_floor",
    [(S_0716, FLOOR_0716), (S_0717, FLOOR_0717)],
    ids=["2026-07-16", "2026-07-17"],
)
def test_recorded_smalln_sessions_admit_ati_eme_bwxt(scores, recorded_floor) -> None:
    # Status quo first: reproduce the recorded all-veto bit-for-bit.
    ctx, floor, _, kept = _run_kernel(scores, buy_floor_min=0.20)
    assert floor == recorded_floor
    assert kept == []
    assert set(ctx._blocked_by_ticker) == {t for t, _ in scores}

    # Guarded: floor relaxes to the 0.50 anchor; exactly {ATI, EME, BWXT}
    # admitted, {XLI, XLY} still vetoed below 0.50.
    ctx, floor, label, kept = _run_kernel(scores, buy_floor_min=0.20, **GUARD)
    assert floor == 0.50
    assert sorted(kept) == ["ATI", "BWXT", "EME"]
    assert ctx._blocked_by_ticker == {
        "XLI": "veto:rank_score_below_floor",
        "XLY": "veto:rank_score_below_floor",
    }
    assert label == (
        f"smalln-relax(n=5 < N0, min(mode={recorded_floor:.3f}, "
        f"abs=0.50)) = 0.500"
    )


# ────────────────────────────────────────────────────────────────────────────
# (c) config matrix: half-config + out-of-bounds → loud rejection, status quo
# ────────────────────────────────────────────────────────────────────────────

BAD_CONFIGS = [
    pytest.param({"buy_floor_min_n": 12}, "buy_floor_absolute_smalln", id="half-min_n-only"),
    pytest.param({"buy_floor_absolute_smalln": 0.50}, "buy_floor_min_n", id="half-abs-only"),
    pytest.param({"buy_floor_min_n": 1, "buy_floor_absolute_smalln": 0.50}, "buy_floor_min_n", id="min_n=1"),
    pytest.param({"buy_floor_min_n": 100, "buy_floor_absolute_smalln": 0.50}, "buy_floor_min_n", id="min_n=100"),
    pytest.param({"buy_floor_min_n": 12.5, "buy_floor_absolute_smalln": 0.50}, "buy_floor_min_n", id="min_n=12.5-noninteger"),
    pytest.param({"buy_floor_min_n": True, "buy_floor_absolute_smalln": 0.50}, "buy_floor_min_n", id="min_n=bool"),
    pytest.param({"buy_floor_min_n": 12, "buy_floor_absolute_smalln": 0}, "buy_floor_absolute_smalln", id="abs=0"),
    pytest.param({"buy_floor_min_n": 12, "buy_floor_absolute_smalln": 1.2}, "buy_floor_absolute_smalln", id="abs=1.2"),
    pytest.param({"buy_floor_min_n": 12, "buy_floor_absolute_smalln": float("nan")}, "buy_floor_absolute_smalln", id="abs=NaN"),
    pytest.param({"buy_floor_min_n": 12, "buy_floor_absolute_smalln": "0.5"}, "buy_floor_absolute_smalln", id="abs=string"),
]


@pytest.mark.parametrize("bad_cfg,offending_key", BAD_CONFIGS)
def test_invalid_guard_config_rejected_loudly_status_quo(
    bad_cfg, offending_key, caplog
) -> None:
    """Every invalid combination: ERROR log naming the offending key, and a
    floor bit-identical to the run with NO guard keys at all — on the
    recorded 07-16 set the misconfigured guard must NOT admit anything."""
    _, floor_base, label_base, kept_base = _run_kernel(S_0716, buy_floor_min=0.20)
    with caplog.at_level(logging.ERROR):
        _, floor, label, kept = _run_kernel(S_0716, buy_floor_min=0.20, **bad_cfg)
    assert floor == floor_base
    assert label == label_base            # no smalln-relax label on rejection
    assert kept == kept_base == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "invalid guard config must log at ERROR"
    assert any(
        offending_key in r.getMessage() and "REJECTED" in r.getMessage()
        for r in errors
    )


def test_guard_absent_entirely_is_silent_status_quo(caplog) -> None:
    """Both keys absent → guard fully off: bit-identical floor, NO error."""
    with caplog.at_level(logging.ERROR):
        _, floor, label, kept = _run_kernel(S_0716, buy_floor_min=0.20)
    assert floor == FLOOR_0716
    assert kept == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_rejection_is_evaluated_even_at_normal_n(caplog) -> None:
    """Validation is per-run (§2.2), not gated on n < N0: a bad config on a
    normal-n scan still logs ERROR while the floor stays status quo."""
    rows, _ = _recorded_0710()
    _, floor_base, _, _ = _run_kernel(rows, buy_floor_min=0.20)
    with caplog.at_level(logging.ERROR):
        _, floor, _, _ = _run_kernel(
            rows, buy_floor_min=0.20, buy_floor_min_n=1,
            buy_floor_absolute_smalln=0.50,
        )
    assert floor == floor_base
    assert any("REJECTED" in r.getMessage() for r in caplog.records)


# ────────────────────────────────────────────────────────────────────────────
# (d) NaN candidates are excluded from the guard's n (finite-score count)
# ────────────────────────────────────────────────────────────────────────────


def test_nan_scores_excluded_from_guard_n() -> None:
    """12 finite + 3 NaN: finite n == 12 == N0 → guard INACTIVE even though
    len(candidates)=15. Dropping one finite score (11 finite + 3 NaN)
    activates the small-n branch. NaN drop semantics unchanged throughout."""
    finite = [0.52 + 0.005 * i for i in range(12)]     # mean+σ > 0.50
    nans = [("NAN1", float("nan")), ("NAN2", float("nan")), ("NAN3", float("nan"))]
    rows12 = _pairs(finite) + nans
    _, floor_base, label_base, _ = _run_kernel(finite, buy_floor_min=0.20)
    ctx, floor, label, kept = _run_kernel(rows12, buy_floor_min=0.20, **GUARD)
    assert floor == floor_base            # n_finite=12 not < 12 → status quo
    assert label == label_base
    assert all(
        ctx._blocked_by_ticker[t] == "veto:rank_score_nan"
        for t in ("NAN1", "NAN2", "NAN3")
    )

    rows11 = _pairs(finite[:11]) + nans   # finite n=11 < 12 → guard active
    ctx, floor, label, _ = _run_kernel(rows11, buy_floor_min=0.20, **GUARD)
    assert label.startswith("smalln-relax(n=11 < N0")
    assert floor == 0.50
    assert ctx._blocked_by_ticker["NAN1"] == "veto:rank_score_nan"


def test_none_scores_not_counted_and_still_kept() -> None:
    """None (unscored) candidates: kept per contract, and not counted in n."""
    rows = _pairs(S_0716) + [("NONE1", None)]
    ctx, floor, label, kept = _run_kernel(rows, buy_floor_min=0.20, **GUARD)
    assert label.startswith("smalln-relax(n=5 < N0")   # n stays 5, not 6
    assert floor == 0.50
    assert "NONE1" in kept                              # unscored passthrough


# ────────────────────────────────────────────────────────────────────────────
# (e) all three adaptive modes' small-n branches
# ────────────────────────────────────────────────────────────────────────────


def test_smalln_branch_adaptive_mean_std() -> None:
    _, floor, label, kept = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std", buy_floor_min=0.20, **GUARD
    )
    assert floor == 0.50
    assert sorted(kept) == ["ATI", "BWXT", "EME"]
    assert "smalln-relax" in label


def test_smalln_branch_adaptive_quantile() -> None:
    # Status quo q0.80 on n=5: floor ≈ 0.5497 admits ATI only.
    _, floor_base, _, kept_base = _run_kernel(
        S_0716, raw_floor="adaptive_quantile", buy_floor_min=0.20
    )
    assert kept_base == ["ATI"]
    # Guarded: relaxes to 0.50 → breadth backstop admits the three stocks.
    _, floor, label, kept = _run_kernel(
        S_0716, raw_floor="adaptive_quantile", buy_floor_min=0.20, **GUARD
    )
    assert floor == 0.50 < floor_base
    assert sorted(kept) == ["ATI", "BWXT", "EME"]
    assert "smalln-relax" in label


def test_smalln_branch_adaptive_mean_std_cap() -> None:
    # cap=0.60 not binding → F_mode = mean+σ = 0.5611… relaxes to 0.50.
    _, floor, label, kept = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap",
        buy_floor_min=0.20, buy_floor_adaptive_cap=0.60, **GUARD
    )
    assert floor == 0.50
    assert sorted(kept) == ["ATI", "BWXT", "EME"]
    assert "smalln-relax" in label

    # Default cap=0.30 already BELOW abs=0.50 → relax-only no-op: floor
    # stays at the status-quo 0.30 (min() can only lower, never raise).
    _, floor_base, _, kept_base = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap", buy_floor_min=0.20
    )
    _, floor, _, kept = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap", buy_floor_min=0.20, **GUARD
    )
    assert floor == floor_base == 0.30
    assert kept == kept_base


def test_smalln_below_stats_fallback_n1_still_relax_only() -> None:
    """n=1 (< 2, mode stats fallback): F_mode = min_fl; the guard's
    max(min_fl, min(F_mode, abs)) degrades to exactly min_fl — no change."""
    _, floor_base, _, kept_base = _run_kernel([0.55], buy_floor_min=0.20)
    _, floor, _, kept = _run_kernel([0.55], buy_floor_min=0.20, **GUARD)
    assert floor == floor_base == 0.20
    assert kept == kept_base == ["T000"]


# ────────────────────────────────────────────────────────────────────────────
# (f) one-sidedness: synthetic compressed sets + pathological cap < min
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize(
    "scores",
    [COMPRESSED_045, COMPRESSED_055],
    ids=["compressed-0.45", "compressed-0.55"],
)
def test_one_sidedness_on_compressed_scales(mode, scores) -> None:
    """RELAX-ONLY invariant: on Platt-compressed scales (range 0.07) every
    mode's guarded floor is <= status quo and the guarded admitted set is a
    superset — the r1 hard-switch would have INVENTED all-vetoes here."""
    _, floor_base, _, kept_base = _run_kernel(
        scores, raw_floor=mode, buy_floor_min=0.20
    )
    _, floor_g, _, kept_g = _run_kernel(
        scores, raw_floor=mode, buy_floor_min=0.20, **GUARD
    )
    assert floor_g <= floor_base
    assert set(kept_g) >= set(kept_base)
    assert len(kept_g) >= len(kept_base)


def test_pathological_cap_below_min_never_raises_floor() -> None:
    """Approval-note hardening: with the PRE-EXISTING misconfig
    cap(0.10) < buy_floor_min(0.20), the status-quo cap-mode floor is 0.10.
    The RFC §2.1 formula alone would RAISE it to max(min_fl, ·)=0.20; the
    unconditional relax-only clamp must keep it at EXACTLY 0.10 (status
    quo), preserving one-sidedness unconditionally."""
    _, floor_base, _, kept_base = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap",
        buy_floor_min=0.20, buy_floor_adaptive_cap=0.10,
    )
    assert floor_base == 0.10
    _, floor_g, label_g, kept_g = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap",
        buy_floor_min=0.20, buy_floor_adaptive_cap=0.10, **GUARD
    )
    assert floor_g == 0.10                # NOT 0.20 — never raised
    assert kept_g == kept_base            # admission unchanged
    assert label_g.endswith("= 0.100")


# ────────────────────────────────────────────────────────────────────────────
# kernel/twin LOCKSTEP — panel_scoring.py mirrors the kernel bit-for-bit
# ────────────────────────────────────────────────────────────────────────────

_LOCKSTEP_CASES = [
    pytest.param(S_0716, {}, id="0716-noguard"),
    pytest.param(S_0716, GUARD, id="0716-guard"),
    pytest.param(S_0717, GUARD, id="0717-guard"),
    pytest.param(COMPRESSED_045, GUARD, id="compressed045-guard"),
    pytest.param(COMPRESSED_055, GUARD, id="compressed055-guard"),
]


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize("scores,guard_cfg", _LOCKSTEP_CASES)
def test_twin_floor_matches_kernel_bit_for_bit(mode, scores, guard_cfg) -> None:
    _, floor_k, label_k, kept_k = _run_kernel(
        scores, raw_floor=mode, buy_floor_min=0.20, **guard_cfg
    )
    _, floor_t, label_t, kept_t = _run_twin(
        scores, raw_floor=mode, buy_floor_min=0.20, **guard_cfg
    )
    assert floor_t == floor_k             # bit-identical
    assert label_t == label_k
    assert sorted(kept_t) == sorted(kept_k)


def test_twin_floor_matches_kernel_at_n85(caplog) -> None:
    rows, payload = _recorded_0710()
    for guard_cfg in ({}, GUARD):
        _, floor_k, label_k, kept_k = _run_kernel(
            rows, buy_floor_min=0.20, **guard_cfg
        )
        _, floor_t, label_t, kept_t = _run_twin(
            rows, buy_floor_min=0.20, **guard_cfg
        )
        assert floor_t == floor_k == payload["recorded_floor"]
        assert label_t == label_k
        assert sorted(kept_t) == sorted(kept_k)


def test_twin_recorded_0716_admits_ati_eme_bwxt() -> None:
    ctx, floor, label, kept = _run_twin(S_0716, buy_floor_min=0.20, **GUARD)
    assert floor == 0.50
    assert sorted(kept) == ["ATI", "BWXT", "EME"]
    assert ctx.blocked_by == {
        "XLI": "panel_score_below_buy_floor",
        "XLY": "panel_score_below_buy_floor",
    }
    assert "smalln-relax" in label


def test_twin_invalid_config_rejected_loudly_status_quo(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        _, floor, _, kept = _run_twin(
            S_0716, buy_floor_min=0.20,
            buy_floor_min_n=100, buy_floor_absolute_smalln=0.50,
        )
    assert floor == FLOOR_0716
    assert kept == []
    assert any(
        "buy_floor_min_n" in r.getMessage() and "REJECTED" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.ERROR
    )


def test_twin_pathological_cap_below_min_never_raises_floor() -> None:
    _, floor_k, _, _ = _run_kernel(
        S_0716, raw_floor="adaptive_mean_std_cap",
        buy_floor_min=0.20, buy_floor_adaptive_cap=0.10, **GUARD
    )
    _, floor_t, _, _ = _run_twin(
        S_0716, raw_floor="adaptive_mean_std_cap",
        buy_floor_min=0.20, buy_floor_adaptive_cap=0.10, **GUARD
    )
    assert floor_t == floor_k == 0.10
