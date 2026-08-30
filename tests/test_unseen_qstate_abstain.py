"""Unseen / NaN Q-state → the per-ticker model ABSTAINS (2026-08-30).

Forensic on the pinned pipeline (afb73626): ``predict_qlearning`` returned
``raw_score = 0.0`` for a NaN feature or a never-visited Q-row, and the
isotonic ER calibrator mapped 0.0 to a POSITIVE expected return (×12 at the
60d rotation horizon). 3 of 11 live buys 2026-08-18..28 fired on
``raw_score == 0.0``. This file pins the abstain contract end to end:

  * unseen state / NaN feature → ``predict_qlearning`` returns None
  * ``score_artifact`` → ``abstain_result``: raw/rank/ER all None, signal
    "abstain"; the calibrator is never evaluated
  * a candidate that abstains is dropped with ``er_abstain_unseen_state``
    (not a buy, not a rotation buy-leg), even under ``bypass_ticker_gate``
  * a held name that abstains carries ER None → no model_sell strike, no
    model_protection strike, no model_sell fire
  * the ×(horizon/lookahead) extrapolation is reported once per run
"""
from __future__ import annotations

import datetime
import logging
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from renquant_pipeline.kernel import models
from renquant_pipeline.kernel.models import (
    REASON_ER_ABSTAIN_UNSEEN_STATE,
    expected_return_from_calibration,
    horizon_extrapolation_report,
    predict_qlearning,
    score_artifact,
    warn_horizon_extrapolation,
)

FEATS = ["f1", "f2"]
N_BINS = 2
# state = ((bin_f1 * 2) + bin_f2) * 3 + holding_bucket; holding_bucket:
# 1 = flat (candidate scoring), 2 = long (held scoring).
VISITED_FLAT = 1     # f1<0.5, f2<0.5, flat  → the only row training touched
VISITED_HELD = 2     # same feature state, held

# The live trap, in miniature: er_y(raw=0.0) is POSITIVE (+0.02 over 5d).
CALIBRATION = {
    "method": "isotonic",
    "x_thresholds": [-1.0, 0.0, 1.0],
    "y_thresholds": [0.1, 0.5, 0.9],
    "base_rate": 0.3,
    "er_method": "isotonic",
    "er_x_thresholds": [-1.0, 0.0, 1.0],
    "er_y_thresholds": [-0.05, 0.02, 0.06],
    "er_lookahead": 5,
}


def _artifact() -> dict:
    q = np.zeros((N_BINS * N_BINS * 3, 3))
    q[VISITED_FLAT] = [0.5, 0.1, 0.0]     # Q(buy) − Q(sell) = +0.4 → buy
    q[VISITED_HELD] = [0.1, 0.6, 0.0]     # Q(buy) − Q(sell) = −0.5 → sell
    return {
        "policy_type": "qlearning",
        "feature_columns": FEATS,
        # digitize(x, edges) - 1, clipped to [0, n_bins-1]: 0.2 → bin 0, 0.9 → bin 1
        "bin_edges": {"f1": [0.0, 0.5], "f2": [0.0, 0.5]},
        "n_bins": N_BINS,
        "q_table": q.tolist(),
        "buy_threshold": 0.1,
        "sell_threshold": -0.1,
        "score_calibration": dict(CALIBRATION),
    }


VISITED_ROW = pd.Series({"f1": 0.2, "f2": 0.2})
UNSEEN_ROW = pd.Series({"f1": 0.9, "f2": 0.2})       # bin_f1=1 → never visited
NAN_ROW = pd.Series({"f1": float("nan"), "f2": 0.2})


# ── the trap this fix closes (documented, not asserted away) ────────────

def test_pre_fix_shape_zero_raw_maps_to_positive_er_times_twelve():
    """Why 0.0 is not neutral: er_y(0.0) > 0, and the horizon scales it ×12."""
    er5 = expected_return_from_calibration(0.0, CALIBRATION)
    er60 = expected_return_from_calibration(0.0, CALIBRATION, horizon_days=60)
    assert er5 == pytest.approx(0.02)
    assert er60 == pytest.approx(0.02 * 12)


# ── predict_qlearning ───────────────────────────────────────────────────

def test_unseen_state_returns_none():
    assert predict_qlearning(_artifact(), UNSEEN_ROW, holdings=0) is None


def test_nan_feature_returns_none_not_zero():
    assert predict_qlearning(_artifact(), NAN_ROW, holdings=0) is None


def test_visited_state_still_scores():
    assert predict_qlearning(_artifact(), VISITED_ROW, holdings=0) == pytest.approx(0.4)
    assert predict_qlearning(_artifact(), VISITED_ROW, holdings=1) == pytest.approx(-0.5)


def test_holdings_bucket_selects_a_different_q_cell():
    """The candidate (flat) and held cells are distinct rows — the held cell
    of the same feature state may be unseen while the flat one is visited."""
    art = _artifact()
    art["q_table"][VISITED_HELD] = [0.0, 0.0, 0.0]
    assert predict_qlearning(art, VISITED_ROW, holdings=0) == pytest.approx(0.4)
    assert predict_qlearning(art, VISITED_ROW, holdings=1) is None


# ── expected_return_from_calibration / score_artifact ───────────────────

def test_expected_return_none_on_absent_score_never_touches_interp(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("np.interp evaluated on an absent raw score")
    monkeypatch.setattr(models.np, "interp", _boom)
    assert expected_return_from_calibration(None, CALIBRATION, horizon_days=60) is None
    assert expected_return_from_calibration(float("nan"), CALIBRATION) is None


def test_score_artifact_abstains_without_calibrating(monkeypatch):
    calls = []
    monkeypatch.setattr(models, "calibrate_score",
                        lambda *a, **k: calls.append(("cal", a)) or 0.5)
    monkeypatch.setattr(models, "expected_return_from_calibration",
                        lambda *a, **k: calls.append(("er", a)) or 0.1)
    sr = score_artifact(_artifact(), UNSEEN_ROW, holdings=0, horizon_days=60)
    assert sr.abstained
    assert sr.signal == "abstain"
    assert sr.raw_score is None and sr.rank_score is None
    assert sr.expected_return is None
    assert sr.abstain_reason == "unseen_state"
    assert calls == [], "calibrator / ER map must never see an abstain"

    sr2 = score_artifact(_artifact(), VISITED_ROW, holdings=0, horizon_days=60)
    assert not sr2.abstained and sr2.signal == "buy"
    assert [c[0] for c in calls] == ["cal", "er"]


def test_score_artifact_visited_state_er_unchanged():
    sr = score_artifact(_artifact(), VISITED_ROW, holdings=0, horizon_days=60)
    # interp(0.4) on [-1,0,1]→[-0.05,0.02,0.06] = 0.02 + 0.4*0.04 = 0.036; ×12
    assert sr.expected_return == pytest.approx(0.036 * 12)
    assert sr.raw_score == pytest.approx(0.4)


# ── candidate path: ScoreBuyTask ────────────────────────────────────────

def _cand_tc(row: pd.Series, *, bypass: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ticker="XYZ",
        model=_artifact(),
        config={
            "rotation": {"target_horizon_days": 60},
            "ranking": {"panel_scoring": {"enabled": True,
                                          "bypass_ticker_gate": bypass}},
        },
        features=pd.DataFrame([row.to_dict()]),
        model_action="hold",
        blocked_by=None,
        candidate=None,
    )


@pytest.mark.parametrize("bypass", [False, True])
def test_abstaining_candidate_is_dropped_with_reason(bypass):
    from renquant_pipeline.kernel.pipeline.task_candidates import ScoreBuyTask
    tc = _cand_tc(UNSEEN_ROW, bypass=bypass)
    assert ScoreBuyTask().run(tc) is False
    assert tc.blocked_by == REASON_ER_ABSTAIN_UNSEEN_STATE == "er_abstain_unseen_state"
    assert tc.model_action == "abstain"
    assert tc._raw_score is None
    assert tc._rank_score is None
    assert tc._expected_return is None
    assert tc._expected_return_horizon_days is None


def test_visited_candidate_is_not_dropped():
    from renquant_pipeline.kernel.pipeline.task_candidates import ScoreBuyTask
    tc = _cand_tc(VISITED_ROW, bypass=False)
    assert ScoreBuyTask().run(tc) is None
    assert tc.blocked_by is None
    assert tc._raw_score == pytest.approx(0.4)
    assert tc._expected_return_horizon_days == 60


def test_abstain_never_admits_a_long():
    """The dropped candidate never reaches the long gate; if one ever did with
    ER None, the gate's own contract (ER absent → no ER conjunct) would admit
    on raw alone — which is exactly why the drop happens at ScoreBuyTask."""
    from renquant_pipeline.kernel.pipeline.task_candidates import (
        AssembleCandidateTask, ScoreBuyTask,
    )
    tc = _cand_tc(UNSEEN_ROW, bypass=True)
    tc.rs_score = 0.0
    ScoreBuyTask().run(tc)
    # Assembly is None-safe should a chain ever run it after an abstain.
    AssembleCandidateTask().run(tc)
    assert tc.candidate.expected_return is None
    assert "raw=none" in tc.candidate.detail and "er=none" in tc.candidate.detail


# ── held path: ScoreModelTask + streak + protection ─────────────────────

TUE = datetime.date(2026, 8, 25)     # NYSE trading day


def _held_tc(row: pd.Series) -> SimpleNamespace:
    from renquant_pipeline.kernel.exits import HoldingState
    hs = HoldingState(entry_price=100.0, entry_date=datetime.date(2026, 6, 1),
                      high_watermark=110.0, sell_streak=2,
                      rank_score=0.7, expected_return=0.05,
                      expected_return_horizon_days=60)
    frame = pd.DataFrame([row.to_dict()], index=[pd.Timestamp(TUE)])
    return SimpleNamespace(
        ticker="XYZ", model=_artifact(), today=TUE,
        ohlcv={"SPY": object(), "XYZ": object()},
        config={"rotation": {"target_horizon_days": 60}},
        feature_cache_frame=frame, features=None, holding=hs,
        model_action="hold",
    )


def test_held_abstain_clears_er_and_counts_no_strike():
    from renquant_pipeline.kernel.exits import check_model_sell
    from renquant_pipeline.kernel.model_protection import (
        ACTION_HOLD, ProtectionConfig, ProtectionState, evaluate,
    )
    from renquant_pipeline.kernel.pipeline.task_sell import ScoreModelTask

    tc = _held_tc(UNSEEN_ROW)
    ScoreModelTask().run(tc)
    assert tc.model_action == "abstain"
    assert tc.holding.expected_return is None
    assert tc.holding.rank_score is None
    assert tc.holding.expected_return_horizon_days is None

    # model_sell: streak neither increments nor resets, and never fires.
    st, sig = check_model_sell("abstain", tc.holding, 3, 0, TUE)
    assert st.sell_streak == 2 and not sig.should_exit
    st.sell_streak = 3
    st, sig = check_model_sell("abstain", st, 3, 0, TUE)
    assert st.sell_streak == 3 and not sig.should_exit

    # model_protection: a None μ is "mu_unavailable" — no breach counted.
    cfg = ProtectionConfig(enabled=True, exit_mu_threshold=0.0, n_strikes=3)
    action, state, reason = evaluate(tc.holding.expected_return, cfg,
                                     ProtectionState(2))
    assert action == ACTION_HOLD and state.consecutive_breaches == 2
    assert reason == "mu_unavailable"


def test_held_visited_state_scores_and_strikes_as_before():
    from renquant_pipeline.kernel.exits import check_model_sell
    from renquant_pipeline.kernel.pipeline.task_sell import ScoreModelTask
    tc = _held_tc(VISITED_ROW)
    ScoreModelTask().run(tc)
    assert tc.model_action == "sell"
    assert tc.holding.expected_return is not None
    assert math.isfinite(tc.holding.expected_return)
    st, sig = check_model_sell("sell", tc.holding, 3, 0, TUE)
    assert st.sell_streak == 3 and sig.should_exit


# ── horizon extrapolation visibility ────────────────────────────────────

def test_horizon_extrapolation_report_flags_5_vs_60():
    rep = horizon_extrapolation_report({"A": _artifact(), "B": {"score_calibration": None}}, 60)
    assert rep == {
        "target_horizon_days": 60, "n_calibrated_models": 1, "n_flagged": 1,
        "er_lookaheads": [5], "max_factor": pytest.approx(12.0),
    }
    # lookahead >= horizon/2 → nothing to report
    assert horizon_extrapolation_report({"A": _artifact()}, 10) is None
    assert horizon_extrapolation_report({}, 60) is None


def test_warn_horizon_extrapolation_logs_once_per_call(caplog):
    caplog.set_level(logging.WARNING, logger="kernel.models")
    rep = warn_horizon_extrapolation({"A": _artifact()},
                                     {"rotation": {"target_horizon_days": 60}})
    assert rep["max_factor"] == pytest.approx(12.0)
    lines = [r for r in caplog.records if "ER_HORIZON_EXTRAPOLATION" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    assert "x12.0" in lines[0].getMessage()
    caplog.clear()
    assert warn_horizon_extrapolation({"A": _artifact()},
                                      {"rotation": {"target_horizon_days": 8}}) is None
    assert not [r for r in caplog.records if "ER_HORIZON_EXTRAPOLATION" in r.getMessage()]
