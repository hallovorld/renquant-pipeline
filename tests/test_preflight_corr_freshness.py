"""P-CORR-FRESHNESS — the orch#1065 staleness alarm (soft, never blocking).

Covered by CI: ``.github/workflows/ci.yml`` runs ``make test`` →
``pytest -q`` over ``testpaths = ["tests"]``, so this file is collected
without being named anywhere.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from renquant_pipeline.kernel.preflight import _LEGACY_CHECK_ORDER, run_preflight
from renquant_pipeline.kernel.preflight_pipeline import PreflightContext
from renquant_pipeline.kernel.preflight_pipeline.tasks import (
    correlation_freshness as cf,
)

TODAY = dt.date(2026, 8, 28)
# The measured orch#1065 stamp: the served artifact said 2026-05-22 while a
# fresh one sat one directory up. On 2026-08-28 that is 98 calendar days.
INCIDENT_AS_OF = "2026-05-22"
# NYSE sessions strictly after 2026-05-22 through 2026-08-28: weekdays
# 2026-05-26..2026-08-28 = 69, minus Juneteenth (06-19) and Independence Day
# observed (07-03) = 67. The weekday fallback counts 70 (05-25 Memorial Day
# plus the two holidays are not skipped).
INCIDENT_AGE_NYSE = 67
INCIDENT_AGE_WEEKDAYS = 70

_MATRIX = {"AAPL": {"AAPL": 1.0, "MSFT": 0.4}, "MSFT": {"AAPL": 0.4, "MSFT": 1.0}}


def _v2(as_of: str | None, **extra) -> dict:
    payload = {"schema_version": 2, "matrix": _MATRIX}
    if as_of is not None:
        payload["as_of_date"] = as_of
        payload["data_window_end"] = as_of
    payload.update(extra)
    return payload


def _write(strategy_dir: Path, payload, rel: str = "prod/watchlist-correlation.json") -> Path:
    p = strategy_dir / "artifacts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return p


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr(cf, "_today", lambda: TODAY)


def _run(tmp_path: Path, config: dict | None = None, run_mode: str | None = None):
    ctx = PreflightContext(config=config or {}, strategy_dir=tmp_path, run_mode=run_mode)
    return cf.CorrelationFreshnessTask().check(ctx)


# ── fresh / stale ───────────────────────────────────────────────────────────

def test_fresh_artifact_passes_and_names_age(tmp_path):
    _write(tmp_path, _v2("2026-08-26"))
    r = _run(tmp_path)
    assert r.name == "P-CORR-FRESHNESS"
    assert r.severity == "soft" and r.ok is True
    # 08-27 (Thu) + 08-28 (Fri) — identical under NYSE and weekday counting.
    assert r.details["age_sessions"] == 2
    assert r.details["max_age_sessions"] == 30
    assert "2026-08-26" in r.message and " 2 " in r.message


def test_incident_stale_artifact_is_soft_not_ok_naming_age(tmp_path):
    p = _write(tmp_path, _v2(INCIDENT_AS_OF))
    r = _run(tmp_path)
    assert r.severity == "soft"       # an ALARM — must never block the run
    assert r.ok is False
    age = r.details["age_sessions"]
    if r.details["calendar"] == "NYSE":
        assert age == INCIDENT_AGE_NYSE
    else:  # calendar backend absent on this machine — fallback is declared
        assert age == INCIDENT_AGE_WEEKDAYS
        assert "WEEKDAYS" in r.message
    assert age > 30
    assert "STALE" in r.message
    assert INCIDENT_AS_OF in r.message
    assert f"{age} " in r.message
    assert "max_age_sessions=30" in r.message
    assert "orch#1065" in r.message
    assert "point the config at the maintained file" in r.message
    assert str(p) in r.message


def test_stamped_today_is_zero_sessions_old(tmp_path):
    _write(tmp_path, _v2(TODAY.isoformat()))
    r = _run(tmp_path)
    assert r.ok is True and r.details["age_sessions"] == 0


def test_boundary_age_equal_to_bound_passes(tmp_path):
    # age == bound is NOT stale (strict '>' per the contract).
    _write(tmp_path, _v2("2026-08-26"))
    r = _run(tmp_path, {"regime": {"correlation_artifact_max_age_sessions": 2}})
    assert r.ok is True and r.details["age_sessions"] == 2


# ── unverifiable stamps ─────────────────────────────────────────────────────

def test_missing_as_of_date_is_unverified_not_fresh(tmp_path):
    _write(tmp_path, _MATRIX)  # legacy v1 flat matrix: no stamp at all
    r = _run(tmp_path)
    assert r.severity == "soft" and r.ok is False
    assert "UNVERIFIED" in r.message
    assert r.details["stamp_field"] is None


def test_garbage_as_of_date_is_unverified(tmp_path):
    _write(tmp_path, _v2("not-a-date"))
    r = _run(tmp_path)
    assert r.severity == "soft" and r.ok is False
    assert "UNVERIFIED" in r.message and "not-a-date" in r.message


def test_numeric_as_of_date_is_unverified(tmp_path):
    _write(tmp_path, _v2(None, as_of_date=20260522))
    r = _run(tmp_path)
    assert r.ok is False and "UNVERIFIED" in r.message


def test_unreadable_artifact_is_unverified(tmp_path):
    _write(tmp_path, "{ not json")
    r = _run(tmp_path)
    assert r.severity == "soft" and r.ok is False
    assert "UNVERIFIED" in r.message


def test_data_window_end_accepted_when_as_of_date_absent(tmp_path):
    _write(tmp_path, {"schema_version": 2, "matrix": _MATRIX,
                      "data_window_end": "2026-08-27"})
    r = _run(tmp_path)
    assert r.ok is True
    assert r.details["stamp_field"] == "data_window_end"
    assert r.details["age_sessions"] == 1


# ── missing file defers to P-CORR-METADATA ──────────────────────────────────

def test_missing_file_defers_to_metadata_rail(tmp_path):
    r = _run(tmp_path)
    assert r.severity == "soft" and r.ok is True
    assert "P-CORR-METADATA" in r.message and "deferred" in r.message


def test_missing_file_defers_in_full_run_mode_too(tmp_path):
    r = _run(tmp_path, run_mode="full")
    assert r.severity == "soft" and r.ok is True


# ── path resolution parity with the correlation guard ───────────────────────

def test_config_relative_path_resolves_under_strategy_artifacts(tmp_path):
    _write(tmp_path, _v2(INCIDENT_AS_OF), rel="watchlist-correlation.json")
    r = _run(tmp_path, {"regime": {"correlation_artifact": "watchlist-correlation.json"}})
    assert r.ok is False and r.details["path"] == str(tmp_path / "artifacts" / "watchlist-correlation.json")


def test_config_absolute_path_passthrough(tmp_path):
    p = tmp_path / "elsewhere" / "corr.json"
    p.parent.mkdir()
    p.write_text(json.dumps(_v2("2026-08-27")))
    r = _run(tmp_path, {"regime": {"correlation_artifact": str(p)}})
    assert r.ok is True and r.details["path"] == str(p)


# ── bound config ────────────────────────────────────────────────────────────

def test_bound_override_relaxes(tmp_path):
    _write(tmp_path, _v2(INCIDENT_AS_OF))
    r = _run(tmp_path, {"regime": {"correlation_artifact_max_age_sessions": 100}})
    assert r.ok is True and r.details["max_age_sessions"] == 100
    assert "bound_note" not in r.details


def test_bound_override_tightens(tmp_path):
    _write(tmp_path, _v2("2026-08-19"))  # 7 sessions old on 08-28
    r = _run(tmp_path, {"regime": {"correlation_artifact_max_age_sessions": 5}})
    assert r.ok is False and r.details["age_sessions"] == 7
    assert "max_age_sessions=5" in r.message


@pytest.mark.parametrize("bad", ["abc", -3, True, None, 2.5, [30]])
def test_malformed_bound_uses_default_and_says_so(tmp_path, bad):
    _write(tmp_path, _v2("2026-08-26"))
    r = _run(tmp_path, {"regime": {"correlation_artifact_max_age_sessions": bad}})
    assert r.ok is True
    assert r.details["max_age_sessions"] == cf.DEFAULT_MAX_AGE_SESSIONS == 30
    assert "malformed" in r.details["bound_note"]
    assert "malformed" in r.message and "default 30" in r.message


def test_zero_bound_is_legal(tmp_path):
    _write(tmp_path, _v2("2026-08-27"))
    r = _run(tmp_path, {"regime": {"correlation_artifact_max_age_sessions": 0}})
    assert r.ok is False and r.details["max_age_sessions"] == 0


# ── calendar fallback is declared, never silent ─────────────────────────────

def test_calendar_backend_failure_falls_back_to_weekdays_and_says_so(tmp_path, monkeypatch):
    import renquant_common.market_calendar as mc

    def _boom(*a, **k):
        raise mc.CalendarUnavailableError("stubbed: pandas_market_calendars missing")

    monkeypatch.setattr(mc, "sessions_between", _boom)
    _write(tmp_path, _v2(INCIDENT_AS_OF))
    r = _run(tmp_path)
    assert r.ok is False
    assert r.details["calendar"] == "weekday-fallback"
    assert r.details["age_sessions"] == INCIDENT_AGE_WEEKDAYS
    assert "WEEKDAYS" in r.message and "stubbed" in r.message


def test_nyse_calendar_skips_holidays_when_available(tmp_path):
    pytest.importorskip("pandas_market_calendars")
    _write(tmp_path, _v2(INCIDENT_AS_OF))
    r = _run(tmp_path)
    assert r.details["calendar"] == "NYSE"
    assert r.details["age_sessions"] == INCIDENT_AGE_NYSE


# ── registration + never-blocking through the real entrypoint ───────────────

def test_registered_right_after_corr_metadata():
    i = _LEGACY_CHECK_ORDER.index("P-CORR-METADATA")
    assert _LEGACY_CHECK_ORDER[i + 1] == "P-CORR-FRESHNESS"


def test_stale_artifact_never_hard_fails_run_preflight(tmp_path):
    _write(tmp_path, _v2(INCIDENT_AS_OF))
    for run_mode in ("full", "sell-only (intraday)"):
        results = run_preflight(config={}, broker=None, strategy_dir=tmp_path,
                                strict=False, run_mode=run_mode)
        names = [r.name for r in results]
        assert names.index("P-CORR-FRESHNESS") == names.index("P-CORR-METADATA") + 1
        r = next(x for x in results if x.name == "P-CORR-FRESHNESS")
        assert r.severity == "soft" and r.ok is False   # alarm raised, not a block
        hard_failed = {x.name for x in results if x.severity == "hard" and not x.ok}
        assert "P-CORR-FRESHNESS" not in hard_failed
