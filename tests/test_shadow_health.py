

# ---------------------------------------------------------------------------
# Two-axis freshness (GOAL-6 decision A, 2026-07-29). The single-axis rule was
# UNSATISFIABLE for a fwd60 recipe: the last training label needs its forward
# window closed, so the cutoff can never be nearer than the horizon. A model
# retrained this morning flagged stale on arrival, and the same rule sat behind
# months of silently refused weekly promotions.
# ---------------------------------------------------------------------------

import datetime as _dt

from renquant_pipeline.kernel.panel_pipeline.shadow_health import (  # noqa: E402
    DEFAULT_CUTOFF_LAG_SLACK_DAYS,
    LOOKAHEAD_FIELD,
    TRAINED_DATE_FIELD,
    finalize_shadow_health,
)

RUN = _dt.date(2026, 7, 29)


def _rec(**kw):
    base = dict(
        loaded=True, artifact_resolved=True, n_candidates=80, n_scored=79,
        coverage_frac=0.99, content_sha256="sha256:abc",
        config_fingerprint="cfg1",
        effective_train_cutoff_date="2026-04-28",     # 92d lag: fwd60's floor
        trained_date="2026-07-27",                    # trained 2 days ago
        lookahead_days=60,
    )
    base.update(kw)
    return base


def test_a_fwd60_model_retrained_today_is_no_longer_stale_on_arrival():
    r = finalize_shadow_health(_rec(), run_date=RUN)
    assert r["actionable"] is True, r["reasons"]
    assert not [x for x in r["reasons"] if x.startswith("stale_")]
    assert r["cutoff_lag_floor_days"] == 84
    assert r["cutoff_lag_bound_days"] == 84 + DEFAULT_CUTOFF_LAG_SLACK_DAYS
    assert r["trained_age_days"] == 2


def test_axis2_still_catches_inputs_that_stopped_advancing():
    # retrains keep succeeding, but the panel froze months ago
    r = finalize_shadow_health(
        _rec(effective_train_cutoff_date="2025-10-01"), run_date=RUN)
    assert r["actionable"] is False
    assert any(x.startswith("cutoff_lag_") for x in r["reasons"]), r["reasons"]


def test_axis1_still_catches_a_retrain_that_stopped():
    # inputs fine, but nothing has been retrained in half a year
    r = finalize_shadow_health(_rec(trained_date="2026-01-05"), run_date=RUN)
    assert r["actionable"] is False
    assert any(x.startswith("trained_") for x in r["reasons"]), r["reasons"]


def test_the_622_day_legacy_lane_still_flags():
    r = finalize_shadow_health(
        _rec(effective_train_cutoff_date="2024-11-14",
             trained_date="2024-11-14"), run_date=RUN)
    assert r["actionable"] is False


def test_absent_horizon_fails_closed_to_the_single_axis_rule():
    r = finalize_shadow_health(_rec(**{LOOKAHEAD_FIELD: None}), run_date=RUN)
    assert r["actionable"] is False
    assert "no_declared_lookahead_single_axis" in r["reasons"]


def test_an_absurd_declared_horizon_is_not_trusted():
    # a recipe claiming 5000 trading days must not buy itself a 20-year gate
    r = finalize_shadow_health(_rec(**{LOOKAHEAD_FIELD: 5000}), run_date=RUN)
    assert "no_declared_lookahead_single_axis" in r["reasons"]


def test_missing_trained_date_is_named_not_ignored():
    r = finalize_shadow_health(_rec(**{TRAINED_DATE_FIELD: None}), run_date=RUN)
    assert "missing_trained_date" in r["reasons"]
    assert r["actionable"] is False


def test_a_fwd20_recipe_gets_a_tighter_bound_than_a_fwd60_one():
    r = finalize_shadow_health(_rec(**{LOOKAHEAD_FIELD: 20}), run_date=RUN)
    assert r["cutoff_lag_floor_days"] == 28
    # 92d lag against a 20d recipe's 56d bound -> correctly flagged
    assert any(x.startswith("cutoff_lag_") for x in r["reasons"])
