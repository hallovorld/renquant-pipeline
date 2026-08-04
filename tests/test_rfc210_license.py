"""Unit contract for kernel.rfc210_license (2026-08-04 sell-only incident).

The license admits a gate-failed artifact ONLY on the exact governance shape:
promotion_basis == "freshness_fallback_rfc210" + parseable trained_date within
the serving SLA. Every malformed twin must refuse — the license fails closed
toward the existing hard fail, never toward admission.
"""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.rfc210_license import (
    DEFAULT_MAX_SERVED_AGE_DAYS,
    evaluate_freshness_fallback_license as ev,
)

TODAY = dt.date(2026, 8, 4)


def _payload(basis="freshness_fallback_rfc210", trained="2026-08-02", **kw):
    p = {
        "trained_date": trained,
        "metadata": {
            "promotion_basis": basis,
            "wf_gate_metadata": {"passed": False},
        },
    }
    p.update(kw)
    return p


def test_the_live_incident_shape_is_served():
    v = ev(_payload(), today=TODAY)
    assert v.served, v.reason
    assert v.provenance == {
        "promotion_basis": "freshness_fallback_rfc210",
        "trained_date": "2026-08-02",
        "age_days": 2,
        "max_served_age_days": DEFAULT_MAX_SERVED_AGE_DAYS,
    }


def test_exactly_at_the_sla_boundary_is_served_one_past_is_not():
    at = ev(_payload(trained="2026-07-07"), today=TODAY)     # 28d
    past = ev(_payload(trained="2026-07-06"), today=TODAY)   # 29d
    assert at.served
    assert not past.served and "aged out" in past.reason


def test_wrong_basis_refuses():
    assert not ev(_payload(basis="manual_promote"), today=TODAY).served
    assert not ev(_payload(basis=None), today=TODAY).served
    assert not ev(_payload(basis=""), today=TODAY).served


def test_missing_basis_everywhere_refuses():
    p = {"trained_date": "2026-08-02", "metadata": {}}
    assert not ev(p, today=TODAY).served


def test_metadata_basis_wins_over_top_level():
    # A top-level decoy must not license an artifact whose metadata says
    # something else — metadata is where the stamper writes.
    p = _payload(basis="something_else")
    p["promotion_basis"] = "freshness_fallback_rfc210"
    assert not ev(p, today=TODAY).served


def test_top_level_basis_accepted_when_metadata_has_no_key():
    p = {"trained_date": "2026-08-02", "metadata": {},
         "promotion_basis": "freshness_fallback_rfc210"}
    v = ev(p, today=TODAY)
    assert v.served, v.reason


def test_trained_date_missing_empty_or_nonstring_refuses():
    assert not ev(_payload(trained=None), today=TODAY).served
    assert not ev(_payload(trained="  "), today=TODAY).served
    assert not ev(_payload(trained=20260802), today=TODAY).served


def test_trained_date_garbage_refuses():
    assert not ev(_payload(trained="last tuesday"), today=TODAY).served


def test_future_trained_date_refuses():
    v = ev(_payload(trained="2026-08-06"), today=TODAY)
    assert not v.served and "future" in v.reason


def test_metadata_trained_date_fallback_works():
    p = _payload()
    del p["trained_date"]
    p["metadata"]["trained_date"] = "2026-08-02"
    assert ev(p, today=TODAY).served


def test_config_override_widens_and_narrows():
    cfg_wide = {"wf_gate": {"rfc210_max_served_age_days": 60}}
    cfg_narrow = {"wf_gate": {"rfc210_max_served_age_days": 1}}
    p = _payload(trained="2026-07-06")  # 29d
    assert ev(p, config=cfg_wide, today=TODAY).served
    assert not ev(_payload(), config=cfg_narrow, today=TODAY).served


def test_config_override_bool_or_nonpositive_is_ignored():
    # True is an int subclass; it must not become a 1-day SLA.
    p = _payload()  # 2d old
    assert ev(p, config={"wf_gate": {"rfc210_max_served_age_days": True}}, today=TODAY).served
    assert ev(p, config={"wf_gate": {"rfc210_max_served_age_days": 0}}, today=TODAY).served
    assert ev(p, config={"wf_gate": {"rfc210_max_served_age_days": -5}}, today=TODAY).served


def test_non_dict_payload_refuses():
    assert not ev(None, today=TODAY).served
    assert not ev([_payload()], today=TODAY).served
    assert not ev("{}", today=TODAY).served
