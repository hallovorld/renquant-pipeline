"""Fill-truth contract tests (orchestrator #484 §7.3 / §8 item 8).

The #484 forensics found 5 ZM "buy_pending" rows in the runs DB and 0 broker
fills — nothing in the DB distinguished a canceled intent from a fill, so
every consumer overcounted buys and basic facts required the broker API.
These tests pin the contract (round 2, post Codex review on #190): attempt
rows are stamped ``submitted`` with the broker order id at write time;
broker-confirmed outcomes are written back via ``record_order_outcomes``
keyed by broker order identity ONLY, under monotonic transition rules that
out-of-order/replayed broker events cannot rewind; unmatched outcomes become
explicit audit entries, never guesses; the schema change is additive (old
DBs migrate, old rows read back with NULL = never reconciled).
"""
from __future__ import annotations

import datetime

from renquant_pipeline.kernel.persistence import (
    FILL_STATUS_CANCELED,
    FILL_STATUS_FILLED,
    FILL_STATUS_SUBMITTED,
    FILL_STATUS_UNKNOWN,
    ensure_schema,
    get_connection,
    normalize_fill_status,
    record_order_outcomes,
    record_pipeline_run,
    record_trades,
)
from renquant_pipeline.kernel.trade_events import (
    build_buy_trade_event,
    build_sell_trade_event,
)

RUN_DATE = datetime.date(2026, 7, 7)


def _conn(tmp_path):
    return get_connection({"persistence": {"enabled": True,
                                           "db_path": str(tmp_path / "runs.db")}})


def _run(conn, run_id="r1"):
    return record_pipeline_run(
        conn, run_type="live", run_date=RUN_DATE, strategy="renquant_104",
        run_id=run_id,
    )


def _fill_rows(conn, ticker):
    return conn.execute(
        """SELECT action, broker_order_id, fill_status, filled_qty,
                  fill_price, fill_updated_at
             FROM trades WHERE ticker = ? ORDER BY rowid""",
        (ticker,),
    ).fetchall()


def _unmatched_audits(conn):
    return conn.execute(
        "SELECT ticker, detail FROM reconciliation_actions"
        " WHERE kind = 'ORDER_OUTCOME_UNMATCHED' ORDER BY action_id",
    ).fetchall()


# ── normalize_fill_status ─────────────────────────────────────────────────────

def test_normalize_fill_status_canonical_aliases_and_unknown():
    assert normalize_fill_status("filled") == "filled"
    assert normalize_fill_status("FILLED") == "filled"
    assert normalize_fill_status("cancelled") == "canceled"
    assert normalize_fill_status("pending_cancel") == "canceled"
    assert normalize_fill_status("new") == "submitted"
    assert normalize_fill_status("accepted") == "submitted"
    assert normalize_fill_status("partial_fill") == "partially_filled"
    # Codex #190: unrecognized broker vocabulary maps to the EXPLICIT
    # unknown state — never something interpretable as canceled/unfilled.
    assert normalize_fill_status("done_for_day") == FILL_STATUS_UNKNOWN
    assert normalize_fill_status("suspended") == FILL_STATUS_UNKNOWN
    assert normalize_fill_status(None) is None
    assert normalize_fill_status("  ") is None


# ── write-time stamping ───────────────────────────────────────────────────────

class TestRecordTradesStamping:
    def test_pending_intent_is_stamped_submitted_with_order_id(self, tmp_path):
        """The ZM shape: a buy_pending attempt row is an intent, not a fill."""
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "shares": 2, "price": 85.68,
            "decision_inputs": {"order_id": "alpaca-zm-0707",
                                "attempt_status": "buy_pending"},
        }])
        (row,) = _fill_rows(conn, "ZM")
        assert row[0] == "buy_pending"
        assert row[1] == "alpaca-zm-0707"          # lifted from decision_inputs
        assert row[2] == FILL_STATUS_SUBMITTED     # derived from *_pending
        assert row[3] is None and row[4] is None   # no fill facts yet

    def test_explicit_fill_fields_pass_through(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "NFLX", "action": "buy", "date": "2026-06-24",
            "shares": 3, "price": 72.62,
            "broker_order_id": "alpaca-nflx-0624",
            "fill_status": "filled", "filled_qty": 3,
            "fill_price": 72.62, "fill_updated_at": "2026-06-24T13:30:00Z",
        }])
        (row,) = _fill_rows(conn, "NFLX")
        assert row[1:] == ("alpaca-nflx-0624", "filled", 3.0, 72.62,
                           "2026-06-24T13:30:00Z")

    def test_plain_executed_row_without_fill_info_stays_null(self, tmp_path):
        """Sim/LEAN rows and legacy producers: no invented outcome."""
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "MU", "action": "buy", "date": "2026-07-07",
            "shares": 1, "price": 1062.0,
        }])
        (row,) = _fill_rows(conn, "MU")
        assert row[1] is None and row[2] is None

    def test_unrecognized_explicit_status_stored_as_unknown(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy", "date": "2026-07-07",
            "broker_order_id": "id-1", "fill_status": "done_for_day",
        }])
        (row,) = _fill_rows(conn, "ZM")
        assert row[2] == FILL_STATUS_UNKNOWN


# ── post-execution outcome write-back ─────────────────────────────────────────

class TestRecordOrderOutcomes:
    def test_canceled_intent_zero_fills_the_zm_case(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "shares": 2, "price": 85.68, "order_id": "alpaca-zm-0707",
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "alpaca-zm-0707",
            "fill_status": "canceled",
            "filled_qty": 0,
            "fill_updated_at": "2026-07-07T22:56:00Z",
        }])
        assert counts["updated"] == 1 and counts["unmatched"] == 0
        (row,) = _fill_rows(conn, "ZM")
        assert row[2] == FILL_STATUS_CANCELED
        assert row[3] == 0.0
        assert row[4] is None  # canceled: no fill price, kept NULL
        # The DB now answers "was ZM bought?" without the broker API.
        filled = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker='ZM' AND fill_status='filled'"
        ).fetchone()[0]
        assert filled == 0

    def test_fill_outcome_updates_qty_and_price(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "NFLX", "action": "buy_pending", "date": "2026-06-24",
            "shares": 3, "price": 72.50, "broker_order_id": "alpaca-nflx-0624",
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "alpaca-nflx-0624",
            "fill_status": "filled", "filled_qty": 3,
            "filled_avg_price": 72.62, "filled_at": "2026-06-24T13:30:00Z",
        }])
        assert counts["updated"] == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED
        assert row[3] == 3.0 and row[4] == 72.62
        assert row[5] == "2026-06-24T13:30:00Z"

    def test_no_ticker_date_guessing_unmatched_is_audited(self, tmp_path):
        """Codex #190: outcome mutation is keyed by broker order identity
        ONLY — no ticker+date fallback (two same-ticker attempts in one day
        would be indistinguishable). Unmatched -> audit entry, row untouched."""
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-06-23",
            "shares": 2, "price": 86.44,  # legacy row: no order id
        }])
        counts = record_order_outcomes(conn, [{
            "ticker": "ZM", "trade_date": "2026-06-23",
            "action": "buy_pending", "fill_status": "canceled",
        }])
        assert counts["unmatched"] == 1 and counts["updated"] == 0
        (row,) = _fill_rows(conn, "ZM")
        assert row[2] == FILL_STATUS_SUBMITTED  # untouched, never guessed
        audits = _unmatched_audits(conn)
        assert len(audits) == 1 and audits[0][0] == "ZM"
        assert "no_broker_order_id" in audits[0][1]

    def test_unknown_order_id_is_audited(self, tmp_path):
        conn = _conn(tmp_path)
        _run(conn)
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "never-seen",
            "fill_status": "canceled",
        }])
        assert counts["unmatched"] == 1
        audits = _unmatched_audits(conn)
        assert len(audits) == 1 and "no_matching_row" in audits[0][1]

    def test_fail_soft_paths_are_counted_not_raised(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "broker_order_id": "known-id",
        }])
        # disabled persistence
        counts = record_order_outcomes(None, [{"broker_order_id": "x",
                                                "fill_status": "canceled"}])
        assert counts == {"updated": 0, "stale": 0, "unmatched": 0,
                          "skipped": 0, "qty_regressed": 0}
        # garbage entries: no status / non-dict -> skipped, never raise
        counts = record_order_outcomes(conn, [
            {"broker_order_id": "known-id"},   # no status
            "not-a-dict",
        ])
        assert counts["skipped"] == 2 and counts["updated"] == 0
        (row,) = _fill_rows(conn, "ZM")
        assert row[2] == FILL_STATUS_SUBMITTED

    def test_run_id_scope_limits_updates(self, tmp_path):
        conn = _conn(tmp_path)
        r1 = _run(conn, "r1")
        r2 = _run(conn, "r2")
        for run_id in (r1, r2):
            record_trades(conn, run_id, [{
                "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
                "shares": 2, "price": 85.68, "broker_order_id": "shared-id",
            }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "shared-id", "fill_status": "canceled",
        }], run_id=r2)
        assert counts["updated"] == 1
        statuses = [r[2] for r in _fill_rows(conn, "ZM")]
        assert statuses == [FILL_STATUS_SUBMITTED, FILL_STATUS_CANCELED]

    def test_default_timestamp_is_stamped(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "broker_order_id": "id-1",
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "id-1", "fill_status": "canceled",
        }])
        assert counts["updated"] == 1
        (row,) = _fill_rows(conn, "ZM")
        assert row[5]  # fill_updated_at defaulted to now-UTC ISO


# ── monotonic transitions (out-of-order / replayed broker events) ─────────────

class TestMonotonicTransitions:
    def _seed(self, tmp_path, order_id="oid-1"):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "NFLX", "action": "buy_pending", "date": "2026-06-24",
            "shares": 3, "price": 72.50, "broker_order_id": order_id,
        }])
        return conn

    def test_late_submitted_never_overwrites_filled(self, tmp_path):
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3, "fill_price": 72.62,
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "accepted",  # late event
        }])
        assert counts["stale"] == 1 and counts["updated"] == 0
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED and row[3] == 3.0

    def test_cancel_never_overwrites_filled(self, tmp_path):
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3, "fill_price": 72.62,
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "canceled",
        }])
        assert counts["stale"] == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED

    def test_partial_then_cancel_retains_executed_qty_and_price(self, tmp_path):
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "partially_filled",
            "filled_qty": 2, "fill_price": 72.60,
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "canceled",
        }])
        assert counts["updated"] == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_CANCELED
        assert row[3] == 2.0 and row[4] == 72.60  # executed facts retained

    def test_filled_qty_never_decreases(self, tmp_path):
        """Codex #190: a filled_qty decrease must be REJECTED/NO-OPED and
        FLAGGED as an anomaly — not silently clamped. Same-rank event (both
        `partially_filled`) so this isolates the qty invariant from the
        rank/stale invariant tested elsewhere."""
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "partially_filled",
            "filled_qty": 2,
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "partially_filled",
            "filled_qty": 1,  # out-of-order smaller partial
        }])
        assert counts["qty_regressed"] == 1   # observable, not silent
        assert counts["stale"] == 0            # same rank — not a rank-stale event
        (row,) = _fill_rows(conn, "NFLX")
        assert row[3] == 2.0                   # never regressed to 1

    def test_filled_qty_regression_at_filled_rank_is_also_flagged(self, tmp_path):
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3, "fill_price": 72.62,
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 2,  # erroneous/duplicated smaller "filled" report
        }])
        assert counts["qty_regressed"] == 1
        assert counts["updated"] == 1  # status re-applied, qty retained
        (row,) = _fill_rows(conn, "NFLX")
        assert row[3] == 3.0

    def test_late_fill_after_recorded_cancel_applies_broker_truth(self, tmp_path):
        conn = self._seed(tmp_path)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "canceled",
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3, "fill_price": 72.62,
        }])
        assert counts["updated"] == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED

    def test_replay_is_idempotent(self, tmp_path):
        conn = self._seed(tmp_path)
        event = {
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3, "filled_avg_price": 72.62,
            "filled_at": "2026-06-24T13:30:00Z",
        }
        record_order_outcomes(conn, [event])
        before = _fill_rows(conn, "NFLX")
        counts = record_order_outcomes(conn, [dict(event)])  # exact replay
        assert counts["updated"] == 1  # applied, but state identical
        assert _fill_rows(conn, "NFLX") == before

    def test_concurrent_replay_orderings_converge(self, tmp_path):
        """Two interleavings of the same event set end in the same state."""
        events = [
            {"broker_order_id": "oid-1", "fill_status": "accepted"},
            {"broker_order_id": "oid-1", "fill_status": "partially_filled",
             "filled_qty": 2, "fill_price": 72.60},
            {"broker_order_id": "oid-1", "fill_status": "filled",
             "filled_qty": 3, "fill_price": 72.62,
             "fill_updated_at": "2026-06-24T13:30:00Z"},
        ]
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()
        conn_a = self._seed(dir_a)
        record_order_outcomes(conn_a, events)
        state_a = _fill_rows(conn_a, "NFLX")

        conn_b = self._seed(dir_b)
        record_order_outcomes(conn_b, list(reversed(events)))
        state_b = _fill_rows(conn_b, "NFLX")

        # both end filled with qty 3 @ 72.62 regardless of arrival order
        assert state_a[0][2:5] == state_b[0][2:5] == ("filled", 3.0, 72.62)

    def test_unknown_broker_state_recorded_as_explicit_unknown(self, tmp_path):
        conn = self._seed(tmp_path)
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "done_for_day",
        }])
        assert counts["updated"] == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_UNKNOWN
        # and a later real outcome still applies (unknown is low-rank)
        record_order_outcomes(conn, [{
            "broker_order_id": "oid-1", "fill_status": "filled",
            "filled_qty": 3,
        }])
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED


# ── backward compatibility (additive schema) ──────────────────────────────────

class TestBackwardCompatibleSchema:
    def _legacy_db(self, tmp_path):
        """A pre-contract DB: today's trades table minus the fill-truth
        columns (exactly what production DBs look like before this change)."""
        import sqlite3

        path = tmp_path / "runs.db"
        conn = sqlite3.connect(path, isolation_level=None)
        ensure_schema(conn)
        conn.execute("DROP INDEX IF EXISTS idx_trades_broker_order")
        for col in ("broker_order_id", "fill_status", "filled_qty",
                    "fill_price", "fill_updated_at"):
            conn.execute(f"ALTER TABLE trades DROP COLUMN {col}")
        conn.execute(
            "INSERT INTO trades (run_id, trade_date, ticker, action, shares, price)"
            " VALUES ('legacy-run', '2026-06-22', 'ZM', 'buy_pending', 2, 84.34)"
        )
        conn.close()
        return path

    def test_legacy_db_migrates_and_old_rows_read_unknown(self, tmp_path):
        self._legacy_db(tmp_path)
        conn = _conn(tmp_path)  # get_connection -> ensure_schema migration
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        assert {"broker_order_id", "fill_status", "filled_qty",
                "fill_price", "fill_updated_at"} <= cols
        row = conn.execute(
            "SELECT fill_status, filled_qty, broker_order_id FROM trades"
            " WHERE run_id='legacy-run'"
        ).fetchone()
        assert row == (None, None, None)  # never reconciled, never assumed filled

    def test_legacy_db_accepts_new_writes_after_migration(self, tmp_path):
        self._legacy_db(tmp_path)
        conn = _conn(tmp_path)
        run_id = _run(conn, "r-new")
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "broker_order_id": "id-1",
        }])
        counts = record_order_outcomes(conn, [{
            "broker_order_id": "id-1", "fill_status": "canceled",
        }])
        assert counts["updated"] == 1

    def test_ensure_schema_idempotent(self, tmp_path):
        conn = _conn(tmp_path)
        ensure_schema(conn)
        ensure_schema(conn)  # no duplicate-column / duplicate-index errors


# ── event builders carry the contract ─────────────────────────────────────────

class TestBuilderPassthrough:
    def test_buy_event_carries_broker_order_id_and_fill_fields(self):
        event = build_buy_trade_event(
            {"ticker": "ZM", "shares": 2, "price": 85.68,
             "order_id": "alpaca-zm-0707", "fill_status": "filled",
             "filled_qty": 2, "filled_avg_price": 85.70},
            date="2026-07-07",
        )
        assert event["broker_order_id"] == "alpaca-zm-0707"
        assert event["fill_status"] == "filled"
        assert event["filled_qty"] == 2
        assert event["fill_price"] == 85.70

    def test_buy_event_defaults_are_none(self):
        event = build_buy_trade_event(
            {"ticker": "ZM", "shares": 2, "price": 85.68}, date="2026-07-07",
        )
        assert event["broker_order_id"] is None
        assert event["fill_status"] is None

    def test_sell_event_lifts_order_id_from_signal_inputs(self):
        class Sig:
            exit_type = "model_protection"
            reason = "mu<=tau"
            quantity = 3
            decision_inputs = {"order_id": "alpaca-sell-0625"}

        class Holding:
            entry_price = 72.62
            entry_date = datetime.date(2026, 6, 24)
            shares = 3

        event = build_sell_trade_event(
            ticker="NFLX", sig=Sig(), holding=Holding(), price=71.3934,
            today=datetime.date(2026, 6, 25), regime="BULL_CALM",
            confidence=0.6, regime_params={}, config={},
        )
        assert event["broker_order_id"] == "alpaca-sell-0625"
        assert event["fill_status"] is None
