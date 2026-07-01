"""FilterStalenessTask — binding DATA CUTOFF keying + fail-closed semantics.

Pins the #210/#213 behaviour: the universe-admission staleness gate keys age on
the binding DATA CUTOFF (``live_train_end`` / the ``DATA_CUTOFF_FIELDS``
precedence), never ``trained_date``. A fresh ``trained_date`` over a stale /
missing data cutoff must NOT admit an offensive (non-held) buy. Held tickers stay
exempt so their sell path stays armed.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.pipeline.job_universe import (
    DATA_CUTOFF_FIELDS,
    FilterStalenessTask,
    UniverseContext,
)

TODAY = dt.date.today()


def _days_ago(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def _art(**meta) -> dict:
    return {"_metadata": dict(meta)}


def _ctx(models, *, held=None, staleness_days=60, tmp_path=None, **extra_cfg):
    config = {"model_staleness_days": staleness_days, **extra_cfg}
    return UniverseContext(
        config=config,
        strategy_dir=tmp_path,
        broker_name=None,
        held_tickers=set(held or []),
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


def test_nonheld_stale_cutoff_dropped_with_age_reason():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(100),
                             trained_date=_days_ago(5))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_100d_limit_60"


def test_nonheld_missing_cutoff_fails_closed():
    # Fresh trained_date but NO binding data cutoff → must NOT fall back to
    # trained_date; drop as data_cutoff_missing. This is the #210 core fix.
    uctx = _ctx({"AAA": _art(trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_missing"


def test_nonheld_unparseable_cutoff_fails_closed():
    uctx = _ctx({"AAA": _art(live_train_end="not-a-date",
                             trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_missing"


def test_nonheld_future_cutoff_fails_closed():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(-5),  # 5d in the future
                             trained_date=_days_ago(1))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "data_cutoff_future"


def test_fresh_trained_date_does_not_rescue_stale_cutoff():
    # The exact bug: trained_date fresh, data cutoff stale → dropped on cutoff.
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(90),
                             trained_date=_days_ago(0))})
    rej = _run(uctx)
    assert "AAA" not in uctx.loaded_models
    assert rej["AAA"] == "stale_90d_limit_60"


# ── Held (sell path) exemption preserved ──────────────────────────────────────

def test_held_missing_cutoff_still_admitted():
    uctx = _ctx({"AAA": _art(trained_date=_days_ago(1))}, held={"AAA"})
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []


def test_held_stale_cutoff_still_admitted():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(200))}, held={"AAA"})
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []


def test_held_future_cutoff_still_admitted():
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(-30))}, held={"AAA"})
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []


# ── Field precedence + configurability ────────────────────────────────────────

def test_panel_style_cutoff_takes_precedence_over_live_train_end():
    # effective_train_cutoff_date ranks above live_train_end in DATA_CUTOFF_FIELDS.
    # Here the higher-precedence field is stale while live_train_end is fresh —
    # the stale higher-precedence axis binds → dropped.
    uctx = _ctx({"AAA": _art(effective_train_cutoff_date=_days_ago(120),
                             live_train_end=_days_ago(5))})
    rej = _run(uctx)
    assert rej["AAA"] == "stale_120d_limit_60"


def test_cutoff_field_precedence_matches_monitor():
    assert DATA_CUTOFF_FIELDS[:] == (
        "effective_selection_cutoff_date",
        "effective_train_cutoff_date",
        "data_cutoff_date",
        "live_train_end",
        "cutoff_date",
    )
    assert "trained_date" not in DATA_CUTOFF_FIELDS


def test_configurable_cutoff_fields_override():
    # Operator points the gate at a custom field; live_train_end is then ignored.
    uctx = _ctx(
        {"AAA": _art(my_cutoff=_days_ago(5), live_train_end=_days_ago(200))},
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


def test_boundary_age_equal_to_limit_admitted():
    # age == limit is NOT > limit → admitted (preserves existing strict-> boundary).
    uctx = _ctx({"AAA": _art(live_train_end=_days_ago(60))}, staleness_days=60)
    _run(uctx)
    assert "AAA" in uctx.loaded_models
    assert uctx.rejections == []
