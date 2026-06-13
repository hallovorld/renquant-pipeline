"""Broker reconciliation state machine tests (eng plan §III.4).

Table-driven across every transition, including the REAL 2026-06-11
STATE-EXT-SELL event (GE/META/HON vanished after a stale state restore).
"""
from __future__ import annotations

from renquant_pipeline.kernel.broker_reconciliation import (
    ADOPT_QTY,
    EXT_SELL,
    FORCED_COVER,
    OK,
    QUARANTINE,
    blocking_tickers,
    client_order_id,
    reconcile,
)


def _kinds(state, broker):
    return {a.ticker: a.kind for a in reconcile(state, broker)}


class TestTransitions:
    def test_real_2026_06_11_ext_sell_event(self):
        # GE/META/HON vanished from the broker after a stale state restore.
        state = {"MU": 1, "GE": 1, "EQIX": 1, "META": 1, "HON": 1}
        broker = {"MU": 1, "EQIX": 1}
        assert _kinds(state, broker) == {
            "GE": EXT_SELL, "META": EXT_SELL, "HON": EXT_SELL,
            "MU": OK, "EQIX": OK}

    def test_quarantine_unknown_position(self):
        assert _kinds({"MU": 1}, {"MU": 1, "TSLA": 5}) == {
            "MU": OK, "TSLA": QUARANTINE}

    def test_adopt_qty_same_sign(self):
        assert _kinds({"MU": 2}, {"MU": 1}) == {"MU": ADOPT_QTY}

    def test_forced_cover_sign_flip_long_to_short(self):
        assert _kinds({"MU": 1}, {"MU": -1}) == {"MU": FORCED_COVER}

    def test_forced_cover_short_to_long(self):
        assert _kinds({"MU": -1}, {"MU": 1}) == {"MU": FORCED_COVER}

    def test_empty(self):
        assert _kinds({}, {}) == {}

    def test_dust_within_tolerance_is_ok(self):
        assert _kinds({"MU": 1.0}, {"MU": 1.0001}) == {"MU": OK}


class TestActionPayload:
    def test_ext_sell_carries_quantities(self):
        a = reconcile({"GE": 3}, {})[0]
        assert a.kind == EXT_SELL and a.state_qty == 3 and a.broker_qty is None

    def test_adopt_carries_both(self):
        a = reconcile({"MU": 5}, {"MU": 2})[0]
        assert a.state_qty == 5 and a.broker_qty == 2


class TestBlockingTickers:
    def test_quarantine_and_forced_cover_block(self):
        acts = reconcile({"MU": 1, "GE": 1}, {"MU": -1, "GE": 1, "TSLA": 9})
        # MU sign-flip (FORCED_COVER), TSLA unknown (QUARANTINE), GE OK
        assert blocking_tickers(acts) == {"MU", "TSLA"}

    def test_ext_sell_does_not_block_new_orders(self):
        # EXT_SELL is a clean exit — the name is tradeable again next run.
        acts = reconcile({"GE": 1}, {})
        assert blocking_tickers(acts) == set()


class TestClientOrderId:
    def test_deterministic(self):
        assert client_order_id("r1", "MU", "SELL", 1.0) == \
            client_order_id("r1", "MU", "SELL", 1.0)

    def test_qty_sensitive(self):
        assert client_order_id("r1", "MU", "SELL", 1.0) != \
            client_order_id("r1", "MU", "SELL", 2.0)

    def test_run_scoped(self):
        assert client_order_id("r1", "MU", "SELL", 1.0) != \
            client_order_id("r2", "MU", "SELL", 1.0)

    def test_intent_sensitive(self):
        assert client_order_id("r1", "MU", "SELL", 1.0) != \
            client_order_id("r1", "MU", "BUY", 1.0)
