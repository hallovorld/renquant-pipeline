"""Alert incident persistence — escalation survives restarts (§12.3)."""
from __future__ import annotations

import datetime as dt

from renquant_pipeline.kernel.alert_lifecycle import AlertBook, CRITICAL, WARN
from renquant_pipeline.kernel.persistence import (
    get_connection,
    load_alert_book,
    save_alert_book,
)

D0 = dt.date(2026, 2, 10)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


class TestRoundTrip:
    def test_save_then_load_preserves_state(self, tmp_path):
        conn = _conn(tmp_path)
        book = AlertBook(escalate_after_days=5)
        for i in range(10):  # escalates to CRITICAL by day 5
            book.observe("staleness", "fund", "max=A", D0 + dt.timedelta(days=i))
        assert save_alert_book(conn, book) == 1
        loaded = load_alert_book(conn, escalate_after_days=5)
        a = loaded.alerts[("staleness", "fund", "max=A")]
        assert a.state == CRITICAL
        assert a.notifications == 2
        assert a.first_seen == D0

    def test_continues_incident_after_restart(self, tmp_path):
        # The whole point: a restart must NOT reset the incident to NEW.
        conn = _conn(tmp_path)
        b1 = AlertBook(escalate_after_days=5)
        for i in range(3):  # day 0,1,2 — still WARN, 1 notification
            b1.observe("x", "s", "h", D0 + dt.timedelta(days=i))
        save_alert_book(conn, b1)
        # "restart": reload, keep observing
        b2 = load_alert_book(conn, escalate_after_days=5)
        a = b2.observe("x", "s", "h", D0 + dt.timedelta(days=6))  # day 6 ≥ 5
        assert a.state == CRITICAL          # escalated using the OLD first_seen
        assert a.notifications == 2         # not re-raised as a fresh NEW

    def test_acked_persists(self, tmp_path):
        conn = _conn(tmp_path)
        book = AlertBook()
        book.observe("x", "s", "h", D0)
        book.ack("x", "s", "h")
        save_alert_book(conn, book)
        loaded = load_alert_book(conn)
        assert loaded.alerts[("x", "s", "h")].acked is True

    def test_upsert_no_duplicate(self, tmp_path):
        conn = _conn(tmp_path)
        book = AlertBook()
        book.observe("x", "s", "h", D0)
        save_alert_book(conn, book)
        book.observe("x", "s", "h", D0 + dt.timedelta(days=1))
        save_alert_book(conn, book)  # second save = update, not insert
        n = conn.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0]
        assert n == 1

    def test_recurrence_after_resolve_resets_first_seen(self, tmp_path):
        # REGRESSION: an incident that RESOLVED and later recurs with the same
        # (audit, scope, cause_hash) is a fresh incident in memory (observe()
        # restarts first_seen). The upsert must persist that reset first_seen —
        # otherwise the reload reads the stale ORIGINAL first_seen and the
        # escalation clock fires immediately on a brand-new incident.
        conn = _conn(tmp_path)
        b1 = AlertBook(escalate_after_days=5)
        b1.observe("x", "s", "h", D0)                       # opens
        b1.resolve_if_absent(seen_today=set(), today=D0 + dt.timedelta(days=10))
        save_alert_book(conn, b1)                           # persisted RESOLVED

        recur = D0 + dt.timedelta(days=100)
        b2 = load_alert_book(conn, escalate_after_days=5)
        b2.observe("x", "s", "h", recur)                    # RESOLVED → fresh WARN
        save_alert_book(conn, b2)

        b3 = load_alert_book(conn, escalate_after_days=5)
        a = b3.alerts[("x", "s", "h")]
        assert a.first_seen == recur                        # reset, not the stale D0
        assert a.state == WARN
        # the restarted clock must NOT escalate two days into the recurrence
        a2 = b3.observe("x", "s", "h", recur + dt.timedelta(days=2))
        assert a2.state == WARN                             # would be CRITICAL pre-fix


class TestNoOps:
    def test_none_conn(self):
        assert save_alert_book(None, AlertBook()) == 0

    def test_empty_book(self, tmp_path):
        assert save_alert_book(_conn(tmp_path), AlertBook()) == 0

    def test_load_empty_returns_empty_book(self, tmp_path):
        book = load_alert_book(_conn(tmp_path))
        assert book.alerts == {}
