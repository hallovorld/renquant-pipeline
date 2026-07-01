"""FilterStalenessTask — multi-axis binding DATA CUTOFF keying + fail-closed.

Pins the #210/#213/#423 behaviour of the universe-admission staleness gate:

* Age keys on the binding DATA CUTOFF (the ``training_data`` / ``selection``
  axes), never ``trained_date``. A fresh ``trained_date`` over a stale / missing
  data cutoff must NOT admit an offensive (non-held) buy.
* Selection freshness and training-data freshness are SEPARATE required facts:
  every present axis is evaluated and a fresh axis never masks a stale one.
* The rejection names the exact offending field.
* The as-of / session date is threaded through ``UniverseContext`` so replay /
  as-of runs are deterministic and never wall-clock-dependent.
* Held names keep the exemption for aging-but-valid models, but an UNTRUSTED
  (missing / unparseable / future) model is NOT admitted wholesale — it is routed
  to ``uctx.fallback_exit`` for a model-independent exit.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.pipeline.job_universe import (
    BASE_CUTOFF_AXES,
    DATA_CUTOFF_FIELDS,
    SELECTION_FIELDS,
    TRAINING_DATA_FIELDS,
    FilterStalenessTask,
    UniverseContext,
)

TODAY = dt.date.today()


def _days_ago(n: int, *, ref: dt.date | None = None) -> str:
    return ((ref or TODAY) - dt.timedelta(days=n)).isoformat()


def _art(**meta) -> dict:
    return {"_metadata": dict(meta)}


def _ctx(models, *, held=None, staleness_days=60, tmp_path=None,
         as_of_date=None, **extra_cfg):
    config = {"model_staleness_days": staleness_days, **extra_cfg}
    return UniverseContext(
        config=config,
        strategy_dir=tmp_path,
        broker_name=None,
        held_tickers=set(held or []),
        as_of_date=as_of_date,
        loaded_models=dict(models),
    )


def _run(uctx: UniverseContext) -> dict:
    assert FilterStalenessTask().run(uctx) is True
    return dict(uctx.rejections)


# ── Non-held (offensive buy) ──────────────────────────────────────────────────

def test_nonheld_fresh_cutoff_admitted():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(10),
                             trained_date=_days_ago(5))})
    rej = _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert rej == {}


def test_nonheld_stale_cutoff_dropped_with_age_and_field():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(100),
                             trained_date=_days_ago(5))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_100d_limit_60:live_train_end"


def test_nonheld_missing_cutoff_fails_closed():
    # Fresh trained_date but NO binding data cutoff → must NOT fall back to
    # trained_date; drop as data_cutoff_missing. This is the #210 core fix.
    uctx = _ctx({"AAA": _art(trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_missing"


def test_nonheld_unparseable_cutoff_fails_closed_naming_field():
    uctx = _ctx({"AAA": _art(live_train_end="not-a-date",
                             trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_unparseable:live_train_end"


def test_nonheld_future_cutoff_fails_closed_naming_field():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(-5),  # 5d in the future
                             trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_future:live_train_end"


def test_fresh_trained_date_does_not_rescue_stale_cutoff():
    # The exact bug: trained_date fresh, data cutoff stale → dropped on cutoff.
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(90),
                             trained_date=_days_ago(0))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_90d_limit_60:live_train_end"


# ── Multi-axis: a fresh axis must NOT mask a stale one (#213/#423 core) ────────

def test_fresh_selection_does_not_mask_stale_training():
    # THE #213/#423 bug: a recent effective_selection_cutoff_date must not hide a
    # stale effective_train_cutoff_date. Both axes are evaluated → dropped on the
    # stale TRAINING axis, naming that field.
    uctx = _ctx({"AAA": _art(effective_selection_cutoff_date=_days_ago(5),
                             effective_train_cutoff_date=_days_ago(120))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_120d_limit_60:effective_train_cutoff_date"


def test_fresh_training_does_not_mask_stale_selection():
    # The inverse: a fresh training cutoff must not hide a stale selection cutoff.
    uctx = _ctx({"AAA": _art(effective_train_cutoff_date=_days_ago(5),
                             effective_selection_cutoff_date=_days_ago(120))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_120d_limit_60:effective_selection_cutoff_date"


def test_future_selection_fails_closed_even_with_fresh_training():
    uctx = _ctx({"AAA": _art(effective_train_cutoff_date=_days_ago(5),
                             effective_selection_cutoff_date=_days_ago(-3))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_future:effective_selection_cutoff_date"


def test_both_axes_fresh_admitted():
    uctx = _ctx({"AAA": _art(effective_train_cutoff_date=_days_ago(10),
                             effective_selection_cutoff_date=_days_ago(8))})
    rej = _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert rej == {}


# ── Held (sell path) exemption — refined for untrusted provenance ─────────────

def test_held_stale_cutoff_still_admitted():
    # Aging-but-VALID cutoff → keep the model armed so the sell path works.
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(200))}, held={"AAA"})
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []
    assert uctx.fallback_exit == []


def test_held_missing_cutoff_routed_to_fallback_exit():
    # Untrusted provenance: NOT admitted wholesale; not hard-rejected either.
    uctx = _ctx({"AAA": _art(trained_date=_days_ago(1))}, held={"AAA"})
    _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert uctx.rejections == []
    assert dict(uctx.fallback_exit) == {"AAA": "data_cutoff_missing"}


def test_held_unparseable_cutoff_routed_to_fallback_exit():
    uctx = _ctx({"AAA": _art(live_train_end="garbage")}, held={"AAA"})
    _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert uctx.rejections == []
    assert dict(uctx.fallback_exit) == {
        "AAA": "data_cutoff_unparseable:live_train_end"
    }


def test_held_future_cutoff_routed_to_fallback_exit():
    # Look-ahead metadata: do NOT keep the scorer active (Codex review point 3).
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(-30))}, held={"AAA"})
    _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert uctx.rejections == []
    assert dict(uctx.fallback_exit) == {
        "AAA": "data_cutoff_future:live_train_end"
    }


def test_held_fresh_cutoff_admitted_no_fallback():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(5))}, held={"AAA"})
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.fallback_exit == []


def test_held_future_axis_beats_stale_axis_for_fallback():
    # A future selection axis (untrusted) wins over a merely-stale training axis:
    # the held name routes to fallback_exit, not admitted-as-stale.
    uctx = _ctx(
        {"AAA": _art(effective_train_cutoff_date=_days_ago(120),
                     effective_selection_cutoff_date=_days_ago(-2))},
        held={"AAA"},
    )
    _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert dict(uctx.fallback_exit) == {
        "AAA": "data_cutoff_future:effective_selection_cutoff_date"
    }


# ── As-of / session date threading (deterministic replay) ─────────────────────

def test_replay_uses_as_of_date_not_wall_clock():
    # Same artifact; admission flips purely on the threaded as-of date, proving
    # the gate does not consult date.today().
    cutoff = "2024-01-01"
    fresh_asof = _ctx({"AAA": _art(live_train_end=cutoff)},
                      as_of_date=dt.date(2024, 1, 31))   # age 30 < 60 → fresh
    _run(fresh_asof)
    assert "AAA" in fresh_asof.loaded_models

    stale_asof = _ctx({"AAA": _art(live_train_end=cutoff)},
                      as_of_date=dt.date(2024, 6, 1))    # age > 60 → stale
    rej = _run(stale_asof)
    assert "AAA" not in stale_asof.loaded_models
    assert rej["AAA"].startswith("stale_") and rej["AAA"].endswith(":live_train_end")


def test_replay_cutoff_after_as_of_is_future_even_if_past_today():
    # 2020-06-01 is long before real TODAY, yet it is AFTER the 2020-01-01 as-of
    # → must be rejected as future. Wall-clock independence.
    uctx = _ctx({"AAA": _art(live_train_end="2020-06-01")},
                as_of_date=dt.date(2020, 1, 1))
    rej = _run(uctx)
    assert rej["AAA"] == "data_cutoff_future:live_train_end"


def test_as_of_datetime_normalized_to_session_date():
    # A tz-aware datetime at the session boundary resolves to its .date() with no
    # off-by-one: cutoff exactly `limit` days before that date is admitted (==).
    session = dt.datetime(2024, 3, 15, 23, 59, tzinfo=dt.timezone.utc)
    cutoff = (session.date() - dt.timedelta(days=60)).isoformat()
    uctx = _ctx({"AAA": _art(live_train_end=cutoff)}, as_of_date=session)
    _run(uctx)
    assert "AAA" in uctx.loaded_models          # age == 60 == limit → admitted
    assert uctx.rejections == []


# ── Field precedence + configurability ────────────────────────────────────────

def test_training_axis_alias_precedence():
    # effective_train_cutoff_date binds ahead of live_train_end within the axis.
    uctx = _ctx({"AAA": _art(effective_train_cutoff_date=_days_ago(120),
                             live_train_end=_days_ago(5))})
    rej = _run(uctx)
    assert rej["AAA"] == "stale_120d_limit_60:effective_train_cutoff_date"


def test_axis_fields_match_monitor_precedence():
    # The gate reads the same fields for the same facts as the orchestrator
    # model_freshness_monitor; the union equals the monitor's flat precedence.
    assert DATA_CUTOFF_FIELDS == (
        "effective_selection_cutoff_date",
        "effective_train_cutoff_date",
        "data_cutoff_date",
        "live_train_end",
        "cutoff_date",
    )
    union = set(TRAINING_DATA_FIELDS) | set(SELECTION_FIELDS)
    assert union == set(DATA_CUTOFF_FIELDS)
    for axis in BASE_CUTOFF_AXES:
        assert "trained_date" not in axis.fields


def test_override_cannot_erase_mandatory_provenance():
    # Operator points the gate at a custom field, but a built-in training field is
    # still present + stale → it BINDS. The override cannot hide mandatory
    # provenance (Codex review, #213).
    uctx = _ctx(
        {"AAA": _art(my_cutoff=_days_ago(5), live_train_end=_days_ago(200))},
        model_staleness_cutoff_fields=["my_cutoff"],
    )
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_200d_limit_60:live_train_end"


def test_override_adds_working_custom_alias():
    # When no built-in training field is present, the operator alias IS honored.
    uctx = _ctx(
        {"AAA": _art(my_cutoff=_days_ago(5))},
        model_staleness_cutoff_fields=["my_cutoff"],
    )
    rej = _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert rej == {}


def test_disabled_when_staleness_days_nonpositive():
    uctx = _ctx({"AAA": _art(trained_date=_days_ago(1))}, staleness_days=0)
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []
    assert uctx.fallback_exit == []


def test_boundary_age_equal_to_limit_admitted():
    # age == limit is NOT > limit → admitted (preserves the strict-> boundary).
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(60))}, staleness_days=60)
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []
