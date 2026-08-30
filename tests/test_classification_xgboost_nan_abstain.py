"""Classification / xgboost per-ticker models ABSTAIN on NaN inputs (2026-08-30).

Follow-up to pipeline#303, which made the qlearning path abstain. Its scope
note: ``predict_classification`` (VLO = Classification, 15 trees) and the
xgboost branch of ``score_artifact`` (NVDA) still mapped a NaN / missing
feature to ``raw = 0.0`` → isotonic ER → a POSITIVE expected return (×12 at
the 60d horizon) — the same trap. This file pins:

  * NaN / missing required feature → ``predict_classification`` returns None
  * NaN / missing feature → ``score_artifact`` → ``abstain_result`` for
    classification AND xgboost: raw/rank/ER None, signal "abstain",
    ``abstain_reason == "nan_features"``; the calibrator and the tree
    walkers are never evaluated
  * a REAL classification vote is byte-identical to the pre-fix value —
    including a vote exactly at the calibrator's neutral value (that is a
    vote, not an unseen state; it is NOT abstained)
  * qlearning: NaN input now reads ``nan_features``; an unseen Q-row is
    still ``unseen_state`` (#303 contract unchanged)
  * ScoreBuyTask drops the abstain with ``er_abstain_nan_features``;
    ScoreModelTask clears the holding's ER / rank (no strike)
  * the Phase 2b breakdown counts per reason
"""
from __future__ import annotations

import datetime
import math
import struct
from types import SimpleNamespace

import pandas as pd
import pytest

from renquant_pipeline.kernel import models
from renquant_pipeline.kernel.models import (
    ABSTAIN_NAN_FEATURES,
    ABSTAIN_UNSEEN_STATE,
    REASON_ER_ABSTAIN_NAN_FEATURES,
    REASON_ER_ABSTAIN_UNSEEN_STATE,
    _traverse_tree,
    abstain_block_reason,
    abstain_breakdown,
    expected_return_from_calibration,
    is_abstain_block_reason,
    missing_features,
    predict_classification,
    score_artifact,
)

FEATS = ["f1", "f2"]

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

# Tree node: [feature_idx, split_value, left_offset, right_offset];
# a leaf has feature_idx == -1 and its vote in split_value.
def _tree(feat: int, split: float, left_vote: float, right_vote: float) -> list:
    return [[feat, split, 1, 2], [-1, left_vote, 0, 0], [-1, right_vote, 0, 0]]


def _clf_artifact(trees: list | None = None) -> dict:
    # 3 trees vote on f1<=0.5 → (1.0, 1.0, 0.7) → mean 0.9 (buy);
    # f1>0.5 → (-1.0, -1.0, 0.7) → mean -0.4333.. (sell)
    return {
        "policy_type": "classification",
        "feature_columns": FEATS,
        "trees": trees if trees is not None else [
            _tree(0, 0.5, 1.0, -1.0),
            _tree(0, 0.5, 1.0, -1.0),
            _tree(1, 0.5, 0.7, 0.7),
        ],
        "buy_threshold": 0.1,
        "sell_threshold": -0.1,
        "score_calibration": dict(CALIBRATION),
    }


def _xgb_model(leaf_low: float, leaf_high: float) -> dict:
    """One tree: split on feature 0 at 0.5; leaves carry the margin."""
    return {"learner": {"gradient_booster": {"model": {"trees": [{
        "left_children":    [1, -1, -1],
        "right_children":   [2, -1, -1],
        "split_conditions": [0.5, 0.0, 0.0],
        "split_indices":    [0, 0, 0],
        "base_weights":     [0.0, leaf_low, leaf_high],
        "default_left":     [1, 0, 0],
    }]}}}}


def _xgb_artifact() -> dict:
    return {
        "policy_type": "xgboost",
        "feature_columns": FEATS,
        "xgb_buy":  _xgb_model(+1.0, -1.0),   # f1<=0.5 → P(buy)=σ(1)
        "xgb_sell": _xgb_model(-1.0, +1.0),   # f1<=0.5 → P(sell)=σ(-1)
        "buy_threshold": 0.1,
        "sell_threshold": -0.1,
        "score_calibration": dict(CALIBRATION),
    }


REAL_ROW = pd.Series({"f1": 0.2, "f2": 0.2})
NAN_ROW = pd.Series({"f1": float("nan"), "f2": 0.2})
MISSING_COL_ROW = pd.Series({"f1": 0.2})            # f2 absent entirely
NONE_ROW = pd.Series({"f1": 0.2, "f2": None}, dtype=object)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# ── the trap this fix closes (documented, not asserted away) ────────────

def test_pre_fix_shape_zero_raw_maps_to_positive_er_times_twelve():
    assert expected_return_from_calibration(0.0, CALIBRATION) == pytest.approx(0.02)
    assert expected_return_from_calibration(0.0, CALIBRATION, horizon_days=60) \
        == pytest.approx(0.24)


# ── missing_features ────────────────────────────────────────────────────

@pytest.mark.parametrize("row,expected", [
    (REAL_ROW, []),
    (NAN_ROW, ["f1"]),
    (MISSING_COL_ROW, ["f2"]),
    (NONE_ROW, ["f2"]),
    (pd.Series({"f1": float("nan")}), ["f1", "f2"]),
    (pd.Series({"f1": "abc", "f2": 0.2}, dtype=object), ["f1"]),
])
def test_missing_features(row, expected):
    assert missing_features(_clf_artifact(), row) == expected


# ── predict_classification ──────────────────────────────────────────────

@pytest.mark.parametrize("row", [NAN_ROW, MISSING_COL_ROW, NONE_ROW])
def test_classification_nan_returns_none_not_zero(row):
    assert predict_classification(_clf_artifact(), row) is None


def _pre_fix_predict_classification(artifact: dict, feature_row: pd.Series) -> float:
    """The pinned (afb73626) implementation, verbatim, minus the NaN branch."""
    feat_cols = artifact["feature_columns"]
    feat_vals = [float(feature_row.get(c, float("nan"))) for c in feat_cols]
    trees = artifact["trees"]
    return sum(_traverse_tree(t, feat_vals) for t in trees) / len(trees)


@pytest.mark.parametrize("row", [
    REAL_ROW,
    pd.Series({"f1": 0.9, "f2": 0.2}),
    pd.Series({"f1": 0.5, "f2": 0.5}),          # exactly on both splits
    pd.Series({"f1": -3.0, "f2": 7.5}),
])
def test_classification_real_votes_byte_identical_to_pre_fix(row):
    art = _clf_artifact()
    got = predict_classification(art, row)
    ref = _pre_fix_predict_classification(art, row)
    assert isinstance(got, float)
    assert struct.pack("<d", got) == struct.pack("<d", ref)


def test_classification_real_vote_values():
    art = _clf_artifact()
    assert predict_classification(art, REAL_ROW) == pytest.approx(0.9)
    assert predict_classification(art, pd.Series({"f1": 0.9, "f2": 0.2})) \
        == pytest.approx((-1.0 - 1.0 + 0.7) / 3)


def test_classification_vote_at_neutral_value_is_a_vote_not_an_abstain(monkeypatch):
    """Two trees vote +1 / -1 on the same row → mean exactly 0.0, which the
    calibrator maps to ER>0. That is a REAL vote: not abstained, scored and
    calibrated exactly as before (only absent INPUT abstains)."""
    art = _clf_artifact(trees=[_tree(0, 0.5, 1.0, -1.0), _tree(0, 0.5, -1.0, 1.0)])
    raw = predict_classification(art, REAL_ROW)
    assert raw == 0.0 and struct.pack("<d", raw) == struct.pack("<d", 0.0)
    sr = score_artifact(art, REAL_ROW, horizon_days=60)
    assert not sr.abstained and sr.abstain_reason is None
    assert sr.signal == "hold"
    assert sr.raw_score == 0.0
    assert sr.rank_score == pytest.approx(0.5)
    assert sr.expected_return == pytest.approx(0.24)


# ── score_artifact: classification ──────────────────────────────────────

@pytest.mark.parametrize("row", [NAN_ROW, MISSING_COL_ROW])
def test_score_artifact_classification_abstains_without_calibrating(monkeypatch, row):
    calls = []
    monkeypatch.setattr(models, "calibrate_score",
                        lambda *a, **k: calls.append("cal") or 0.5)
    monkeypatch.setattr(models, "expected_return_from_calibration",
                        lambda *a, **k: calls.append("er") or 0.1)
    monkeypatch.setattr(models, "_traverse_tree",
                        lambda *a, **k: calls.append("tree") or 0.0)
    sr = score_artifact(_clf_artifact(), row, holdings=0, horizon_days=60)
    assert sr.abstained and sr.signal == "abstain"
    assert sr.raw_score is None and sr.rank_score is None
    assert sr.expected_return is None
    assert sr.abstain_reason == ABSTAIN_NAN_FEATURES == "nan_features"
    assert calls == [], "calibrator / ER map / trees must never see an abstain"


def test_score_artifact_classification_real_vote_unchanged():
    sr = score_artifact(_clf_artifact(), REAL_ROW, holdings=0, horizon_days=60)
    assert not sr.abstained and sr.signal == "buy"
    assert sr.raw_score == pytest.approx(0.9)
    # interp(0.9) on [-1,0,1]→[-0.05,0.02,0.06] = 0.02 + 0.9*0.04 = 0.056; ×12
    assert sr.expected_return == pytest.approx(0.056 * 12)
    assert sr.rank_score == pytest.approx(0.5 + 0.9 * 0.4)


# ── score_artifact: xgboost ─────────────────────────────────────────────

@pytest.mark.parametrize("row", [NAN_ROW, MISSING_COL_ROW, NONE_ROW])
def test_score_artifact_xgboost_abstains_without_calibrating(monkeypatch, row):
    calls = []
    monkeypatch.setattr(models, "calibrate_score",
                        lambda *a, **k: calls.append("cal") or 0.5)
    monkeypatch.setattr(models, "expected_return_from_calibration",
                        lambda *a, **k: calls.append("er") or 0.1)
    monkeypatch.setattr(models, "predict_xgboost",
                        lambda *a, **k: calls.append("xgb") or 0.5)
    sr = score_artifact(_xgb_artifact(), row, holdings=0, horizon_days=60)
    assert sr.abstained and sr.signal == "abstain"
    assert sr.raw_score is None and sr.rank_score is None
    assert sr.expected_return is None
    assert sr.abstain_reason == "nan_features"
    assert calls == [], "calibrator / ER map / booster must never see an abstain"


def test_score_artifact_xgboost_real_row_unchanged():
    sr = score_artifact(_xgb_artifact(), REAL_ROW, holdings=0, horizon_days=60)
    expected_raw = _sigmoid(1.0) - _sigmoid(-1.0)          # ≈ 0.4621
    assert not sr.abstained and sr.signal == "buy"
    assert sr.raw_score == pytest.approx(expected_raw)
    assert sr.expected_return == pytest.approx((0.02 + expected_raw * 0.04) * 12)
    # the other side of the split scores sell, also unchanged
    sr2 = score_artifact(_xgb_artifact(), pd.Series({"f1": 0.9, "f2": 0.2}))
    assert sr2.signal == "sell" and sr2.raw_score == pytest.approx(-expected_raw)


# ── qlearning: reason labels (#303 contract unchanged for unseen rows) ──

def _q_artifact() -> dict:
    import numpy as np
    q = np.zeros((2 * 2 * 3, 3))
    q[1] = [0.5, 0.1, 0.0]
    return {
        "policy_type": "qlearning", "feature_columns": FEATS,
        "bin_edges": {"f1": [0.0, 0.5], "f2": [0.0, 0.5]}, "n_bins": 2,
        "q_table": q.tolist(), "buy_threshold": 0.1, "sell_threshold": -0.1,
        "score_calibration": dict(CALIBRATION),
    }


def test_qlearning_nan_is_nan_features_unseen_row_is_unseen_state():
    assert score_artifact(_q_artifact(), NAN_ROW).abstain_reason == ABSTAIN_NAN_FEATURES
    assert score_artifact(_q_artifact(), MISSING_COL_ROW).abstain_reason == ABSTAIN_NAN_FEATURES
    sr = score_artifact(_q_artifact(), pd.Series({"f1": 0.9, "f2": 0.2}))
    assert sr.abstain_reason == ABSTAIN_UNSEEN_STATE
    assert not score_artifact(_q_artifact(), REAL_ROW).abstained


# ── blocked_by reasons + breakdown ──────────────────────────────────────

def test_abstain_block_reasons():
    assert abstain_block_reason("nan_features") == REASON_ER_ABSTAIN_NAN_FEATURES \
        == "er_abstain_nan_features"
    assert abstain_block_reason("unseen_state") == REASON_ER_ABSTAIN_UNSEEN_STATE \
        == "er_abstain_unseen_state"
    assert is_abstain_block_reason("er_abstain_nan_features")
    assert not is_abstain_block_reason("model_signal:hold")
    assert not is_abstain_block_reason(None)
    assert abstain_breakdown([
        "er_abstain_unseen_state", None, "model_signal:hold",
        "er_abstain_nan_features", "er_abstain_unseen_state",
    ]) == {"er_abstain_unseen_state": 2, "er_abstain_nan_features": 1}
    assert abstain_breakdown([]) == {"er_abstain_unseen_state": 0,
                                     "er_abstain_nan_features": 0}


# ── candidate path: ScoreBuyTask (one per model kind) ───────────────────

def _cand_tc(art: dict, row: pd.Series, *, bypass: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        ticker="XYZ", model=art,
        config={
            "rotation": {"target_horizon_days": 60},
            "ranking": {"panel_scoring": {"enabled": True,
                                          "bypass_ticker_gate": bypass}},
        },
        features=pd.DataFrame([row.to_dict()]),
        model_action="hold", blocked_by=None, candidate=None,
    )


@pytest.mark.parametrize("kind,art", [("classification", _clf_artifact()),
                                      ("xgboost", _xgb_artifact())])
def test_scorebuy_drops_nan_abstain_with_reason(kind, art):
    from renquant_pipeline.kernel.pipeline.task_candidates import ScoreBuyTask
    tc = _cand_tc(art, NAN_ROW, bypass=True)      # bypass does not waive an abstain
    assert ScoreBuyTask().run(tc) is False
    assert tc.blocked_by == REASON_ER_ABSTAIN_NAN_FEATURES == "er_abstain_nan_features"
    assert tc.model_action == "abstain"
    assert tc._raw_score is None and tc._rank_score is None
    assert tc._expected_return is None
    assert tc._expected_return_horizon_days is None

    ok = _cand_tc(art, REAL_ROW, bypass=False)
    assert ScoreBuyTask().run(ok) is None
    assert ok.blocked_by is None and ok.model_action == "buy"
    assert ok._expected_return_horizon_days == 60


# ── held path: ScoreModelTask (one per model kind) ──────────────────────

TUE = datetime.date(2026, 8, 25)


def _held_tc(art: dict, row: pd.Series) -> SimpleNamespace:
    from renquant_pipeline.kernel.exits import HoldingState
    hs = HoldingState(entry_price=100.0, entry_date=datetime.date(2026, 6, 1),
                      high_watermark=110.0, sell_streak=2,
                      rank_score=0.7, expected_return=0.05,
                      expected_return_horizon_days=60)
    frame = pd.DataFrame([row.to_dict()], index=[pd.Timestamp(TUE)])
    return SimpleNamespace(
        ticker="XYZ", model=art, today=TUE,
        ohlcv={"SPY": object(), "XYZ": object()},
        config={"rotation": {"target_horizon_days": 60}},
        feature_cache_frame=frame, features=None, holding=hs,
        model_action="hold",
    )


@pytest.mark.parametrize("kind,art", [("classification", _clf_artifact()),
                                      ("xgboost", _xgb_artifact())])
def test_scoremodel_nan_abstain_clears_er_and_counts_no_strike(kind, art):
    from renquant_pipeline.kernel.exits import check_model_sell
    from renquant_pipeline.kernel.pipeline.task_sell import ScoreModelTask
    tc = _held_tc(art, NAN_ROW)
    ScoreModelTask().run(tc)
    assert tc.model_action == "abstain"
    assert tc.holding.expected_return is None
    assert tc.holding.rank_score is None
    assert tc.holding.expected_return_horizon_days is None
    st, sig = check_model_sell("abstain", tc.holding, 3, 0, TUE)
    assert st.sell_streak == 2 and not sig.should_exit

    ok = _held_tc(art, REAL_ROW)
    ScoreModelTask().run(ok)
    assert ok.model_action == "buy"
    assert ok.holding.expected_return is not None and math.isfinite(ok.holding.expected_return)
    assert ok.holding.expected_return_horizon_days == 60
