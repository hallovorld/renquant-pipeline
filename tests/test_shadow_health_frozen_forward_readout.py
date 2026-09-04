"""A frozen forward readout is stale BY DESIGN — freshness is not its fault.

2026-09-01..03 the live sentinel paged `topdecile_clf_blend_leg` DEGRADED
every session (`cutoff_lag_128d_over_112d(floor_84d+slack_28d)`,
`trained_37d_limit_28d`): the certified classifier is frozen for its
120-session forward ledger (pipeline#213) and MUST NOT be retrained, so the
two-axis freshness rule can never be satisfied for the length of the readout.

The exemption is bound to the certified artifact digest (observed AND
config-pinned), the lane name, and a calendar window; it suppresses only
the freshness tokens and records them. Every other fault class is untouched.
"""
from __future__ import annotations

import datetime as _dt

from renquant_pipeline.kernel.panel_pipeline import shadow_health as sh
from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    FROZEN_FORWARD_READOUTS,
    FrozenForwardReadout,
    finalize_shadow_health,
    frozen_forward_readout_for,
)

CERT = "sha256:1e644354e0981f47"          # the served clf artifact, pinned by strategy-104
RUN = _dt.date(2026, 9, 3)                # the day the page was read


def _clf(**kw):
    """The real 2026-09-03 record shape of the frozen clf lane."""
    base = dict(
        shadow_name="topdecile_clf_blend_leg", kind="xgb",
        loaded=True, artifact_resolved=True, n_candidates=80, n_scored=79,
        coverage_frac=0.99, content_sha256=CERT, expected_content_sha256=CERT,
        config_fingerprint="sha256:1d8f167f", expected_config_fingerprint="sha256:1d8f167f",
        effective_train_cutoff_date="2026-04-28",   # 128d lag on 09-03
        trained_date="2026-07-28",                  # 37d old on 09-03
        lookahead_days=60,
    )
    base.update(kw)
    return base


def test_registry_names_exactly_the_certified_clf_artifact():
    assert len(FROZEN_FORWARD_READOUTS) == 1
    e = FROZEN_FORWARD_READOUTS[0]
    assert isinstance(e, FrozenForwardReadout)
    assert e.lane == "topdecile_clf_blend_leg"
    assert e.content_sha256 == "1e644354e0981f47"
    assert e.frozen_since == _dt.date(2026, 7, 27)
    assert e.until == _dt.date(2027, 3, 31)
    assert "pipeline#213" in e.authority


def test_the_live_frozen_clf_record_is_ok_and_says_why():
    r = finalize_shadow_health(_clf(), run_date=RUN)
    assert r["state"] == sh.STATE_OK and r["actionable"] is True, r["reasons"]
    assert r["reasons"] == []
    f = r["frozen_forward_readout"]
    assert f["lane"] == "topdecile_clf_blend_leg"
    assert f["until"] == "2027-03-31" and f["days_left"] == (_dt.date(2027, 3, 31) - RUN).days
    assert f["freshness_suppressed"] == [
        "cutoff_lag_128d_over_112d(floor_84d+slack_28d)",
        "trained_37d_limit_28d",
    ]
    # the numbers stay in the record for a reader — only the alarm is gone
    assert r["staleness_days"] == 128 and r["trained_age_days"] == 37


def test_unfrozen_lane_with_the_same_numbers_is_still_degraded():
    r = finalize_shadow_health(_clf(shadow_name="some_other_lane"), run_date=RUN)
    assert r["state"] == sh.STATE_DEGRADED
    assert "frozen_forward_readout" not in r
    assert any(x.startswith("cutoff_lag_") for x in r["reasons"])
    assert any(x.startswith("trained_") for x in r["reasons"])


def test_a_swapped_artifact_loses_the_exemption():
    """Observed bytes differ from the certified digest → standing rule (and
    the config pin now mismatches too, which is its own fault)."""
    r = finalize_shadow_health(_clf(content_sha256="sha256:deadbeefdeadbeef"), run_date=RUN)
    assert r["state"] == sh.STATE_DEGRADED
    assert "frozen_forward_readout" not in r
    assert "content_sha256_mismatch" in r["reasons"]
    assert any(x.startswith("cutoff_lag_") for x in r["reasons"])


def test_a_config_that_stops_pinning_the_artifact_loses_the_exemption():
    for pin in (None, "", "sha256:0000000000000000"):
        r = finalize_shadow_health(_clf(expected_content_sha256=pin), run_date=RUN)
        assert "frozen_forward_readout" not in r, pin
        assert any(x.startswith("cutoff_lag_") for x in r["reasons"]), pin


def test_window_edges_are_inclusive_and_self_expiring():
    e = FROZEN_FORWARD_READOUTS[0]
    assert frozen_forward_readout_for(_clf(), run_date=e.frozen_since) is e
    assert frozen_forward_readout_for(_clf(), run_date=e.until) is e
    assert frozen_forward_readout_for(_clf(), run_date=e.frozen_since - _dt.timedelta(days=1)) is None
    after = e.until + _dt.timedelta(days=1)
    assert frozen_forward_readout_for(_clf(), run_date=after) is None
    r = finalize_shadow_health(_clf(), run_date=after)
    assert r["state"] == sh.STATE_DEGRADED and "frozen_forward_readout" not in r


def test_only_freshness_is_suppressed_every_other_fault_stays():
    low = finalize_shadow_health(_clf(coverage_frac=0.10), run_date=RUN)
    assert low["state"] == sh.STATE_DEGRADED
    assert low["reasons"] == ["low_coverage_0.10_min_0.50"] or low["reasons"][0].startswith("low_coverage_")
    assert low["frozen_forward_readout"]["freshness_suppressed"]

    nofp = finalize_shadow_health(_clf(config_fingerprint=None), run_date=RUN)
    assert nofp["state"] == sh.STATE_DEGRADED
    assert nofp["reasons"] == ["missing_config_fingerprint"]

    none = finalize_shadow_health(_clf(n_scored=0, skip_reason="no_scores"), run_date=RUN)
    assert none["state"] == sh.STATE_NOT_SCORED


def test_provenance_defects_are_not_freshness_and_are_not_suppressed():
    missing = finalize_shadow_health(_clf(trained_date=None), run_date=RUN)
    assert "missing_trained_date" in missing["reasons"]
    assert missing["state"] == sh.STATE_DEGRADED
    future = finalize_shadow_health(_clf(trained_date="2027-01-01"), run_date=RUN)
    assert any(x.startswith("trained_date_future_") for x in future["reasons"])
    assert future["state"] == sh.STATE_DEGRADED


def test_digest_forms_a_sixteen_hex_pin_and_a_bare_prefix_agree():
    """The record's observed digest is always the 16-hex ``sha256:`` form
    (``resolve_artifact_identity``); a config pin may omit the prefix."""
    assert frozen_forward_readout_for(_clf(expected_content_sha256="1e644354e0981f47"), run_date=RUN) is not None
    assert frozen_forward_readout_for(_clf(expected_content_sha256="sha256:1E644354E0981F47"), run_date=RUN) is not None
    assert frozen_forward_readout_for(_clf(content_sha256="sha256:1e64435"), run_date=RUN) is None  # < 16 hex never matches
