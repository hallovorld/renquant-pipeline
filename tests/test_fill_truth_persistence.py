"""Fill-truth contract tests (orchestrator #484 §7.3 / §8 item 8).

The #484 forensics found 5 ZM "buy_pending" rows in the runs DB and 0 broker
fills — nothing in the DB distinguished a canceled intent from a fill, so
every consumer overcounted buys and basic facts required the broker API.
These tests pin the contract: attempt rows are stamped ``submitted`` with the
broker order id at write time; broker-confirmed outcomes are written back via
``record_order_outcomes``; the schema change is additive (old DBs migrate,
old rows read back with NULL = unknown).
"""
from __future__ import annotations

import datetime

from renquant_pipeline.kernel.persistence import (
    FILL_STATUS_CANCELED,
    FILL_STATUS_FILLED,
    FILL_STATUS_SUBMITTED,
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


# ── normalize_fill_status ─────────────────────────────────────────────────────

def test_normalize_fill_status_canonical_aliases_and_passthrough():
    assert normalize_fill_status("filled") == "filled"
    assert normalize_fill_status("FILLED") == "filled"
    assert normalize_fill_status("cancelled") == "canceled"
    assert normalize_fill_status("pending_cancel") == "canceled"
    assert normalize_fill_status("new") == "submitted"
    assert normalize_fill_status("accepted") == "submitted"
    assert normalize_fill_status("partial_fill") == "partially_filled"
    # unknown broker states stay visible verbatim (lowercased), not coerced
    assert normalize_fill_status("done_for_day") == "done_for_day"
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


# ── post-execution outcome write-back ─────────────────────────────────────────

class TestRecordOrderOutcomes:
    def test_canceled_intent_zero_fills_the_zm_case(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "shares": 2, "price": 85.68, "order_id": "alpaca-zm-0707",
        }])
        n = record_order_outcomes(conn, [{
            "broker_order_id": "alpaca-zm-0707",
            "fill_status": "canceled",
            "filled_qty": 0,
            "fill_updated_at": "2026-07-07T22:56:00Z",
        }])
        assert n == 1
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
        n = record_order_outcomes(conn, [{
            "broker_order_id": "alpaca-nflx-0624",
            "fill_status": "filled", "filled_qty": 3,
            "filled_avg_price": 72.62, "filled_at": "2026-06-24T13:30:00Z",
        }])
        assert n == 1
        (row,) = _fill_rows(conn, "NFLX")
        assert row[2] == FILL_STATUS_FILLED
        assert row[3] == 3.0 and row[4] == 72.62
        assert row[5] == "2026-06-24T13:30:00Z"

    def test_fallback_match_by_ticker_and_date(self, tmp_path):
        """Rows recorded before order-id stamping are still reconcilable."""
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-06-23",
            "shares": 2, "price": 86.44,
        }])
        n = record_order_outcomes(conn, [{
            "ticker": "ZM", "trade_date": "2026-06-23",
            "action": "buy_pending", "fill_status": "canceled",
        }])
        assert n == 1
        (row,) = _fill_rows(conn, "ZM")
        assert row[2] == FILL_STATUS_CANCELED

    def test_fail_soft_paths(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "shares": 2, "price": 85.68, "broker_order_id": "known-id",
        }])
        # disabled persistence
        assert record_order_outcomes(None, [{"broker_order_id": "x",
                                             "fill_status": "canceled"}]) == 0
        # unknown order id
        assert record_order_outcomes(conn, [{"broker_order_id": "unknown",
                                             "fill_status": "canceled"}]) == 0
        # no match key / no status / garbage entries — skipped, never raise
        assert record_order_outcomes(conn, [
            {"fill_status": "canceled"},
            {"broker_order_id": "known-id"},
            "not-a-dict",
        ]) == 0
        # row untouched
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
        n = record_order_outcomes(conn, [{
            "broker_order_id": "shared-id", "fill_status": "canceled",
        }], run_id=r2)
        assert n == 1
        statuses = [r[2] for r in _fill_rows(conn, "ZM")]
        assert statuses == [FILL_STATUS_SUBMITTED, FILL_STATUS_CANCELED]

    def test_default_timestamp_is_stamped(self, tmp_path):
        conn = _conn(tmp_path)
        run_id = _run(conn)
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "broker_order_id": "id-1",
        }])
        assert record_order_outcomes(conn, [{
            "broker_order_id": "id-1", "fill_status": "canceled",
        }]) == 1
        (row,) = _fill_rows(conn, "ZM")
        assert row[5]  # fill_updated_at defaulted to now-UTC ISO


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
        assert row == (None, None, None)  # unknown, never assumed filled

    def test_legacy_db_accepts_new_writes_after_migration(self, tmp_path):
        self._legacy_db(tmp_path)
        conn = _conn(tmp_path)
        run_id = _run(conn, "r-new")
        record_trades(conn, run_id, [{
            "ticker": "ZM", "action": "buy_pending", "date": "2026-07-07",
            "broker_order_id": "id-1",
        }])
        assert record_order_outcomes(conn, [{
            "broker_order_id": "id-1", "fill_status": "canceled",
        }]) == 1

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
