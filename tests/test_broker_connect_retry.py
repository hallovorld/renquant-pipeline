"""P-BROKER-CONNECT bounded retry, fail-closed preserved.

Regression for the 2026-08-11 07:01 intraday abort: a single transient Alpaca
network blip HARD-failed the whole cycle with no retry. The gate now retries
connect()+get_account_value() a BOUNDED number of times, returns HARD True on
the first success (naming the attempt count when >1), and returns HARD False
ONLY after every attempt fails — a genuine outage must still refuse to trade.

Both the runtime path (``BrokerConnectTask``) and the legacy bridge
(``_check_broker_connect``) share one ``_attempt_broker_connect`` body; these
cover both so the twin implementations cannot drift.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from renquant_pipeline.kernel import preflight
from renquant_pipeline.kernel.preflight import (
    _attempt_broker_connect,
    _check_broker_connect,
)
from renquant_pipeline.kernel.preflight_pipeline.tasks.broker import BrokerConnectTask


class _FlakyBroker:
    """Fails (connect or get_account_value) the first ``fail_times`` attempts,
    then succeeds. ``where`` selects which call raises."""

    def __init__(self, fail_times: int, *, equity: float = 10000.0, where: str = "connect"):
        self.fail_times = fail_times
        self.equity = equity
        self.where = where
        self.attempts = 0  # counts full connect() entries

    def connect(self):
        self.attempts += 1
        if self.where == "connect" and self.attempts <= self.fail_times:
            raise ConnectionError("Read timed out. (read timeout=None)")

    def get_account_value(self):
        if self.where == "account" and self.attempts <= self.fail_times:
            raise ConnectionError("Read timed out. (read timeout=None)")
        return self.equity


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Record backoff sleeps instead of actually sleeping (keeps tests fast and
    lets us assert the retry loop is BOUNDED)."""
    sleeps: list[float] = []
    monkeypatch.setattr(preflight.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_success_first_attempt_has_no_count_suffix(_no_real_sleep):
    broker = _FlakyBroker(fail_times=0)
    res = _check_broker_connect(broker)
    assert res.severity == "hard" and res.ok is True
    assert res.message == "broker connected, equity=$10000.00"
    assert broker.attempts == 1
    assert _no_real_sleep == []  # no retry, no backoff


def test_succeeds_after_transient_failures_names_attempt_count(_no_real_sleep):
    # Fails twice, succeeds on the 3rd — the exact "single blip then recover".
    broker = _FlakyBroker(fail_times=2)
    res = _check_broker_connect(broker, max_attempts=3, backoff_seconds=2.0)
    assert res.severity == "hard" and res.ok is True
    assert "after 3 attempts" in res.message
    assert "equity=$10000.00" in res.message
    assert broker.attempts == 3
    assert _no_real_sleep == [2.0, 2.0]  # bounded: exactly attempts-1 backoffs


def test_get_account_value_failure_is_also_retried(_no_real_sleep):
    broker = _FlakyBroker(fail_times=1, where="account")
    res = _check_broker_connect(broker, max_attempts=3)
    assert res.ok is True
    assert "after 2 attempts" in res.message
    assert broker.attempts == 2


def test_fails_closed_after_all_attempts_exhausted(_no_real_sleep):
    # Always fails -> HARD False (fail-closed): a genuine outage must not trade.
    broker = _FlakyBroker(fail_times=999)
    res = _check_broker_connect(broker, max_attempts=3, backoff_seconds=2.0)
    assert res.severity == "hard"
    assert res.ok is False  # <-- fail-closed preserved
    assert "broker connect failed after 3 attempts" in res.message
    assert "Read timed out" in res.message  # last error surfaced
    assert broker.attempts == 3  # bounded: exactly max_attempts, no more
    assert _no_real_sleep == [2.0, 2.0]  # bounded backoffs


def test_none_broker_is_soft_skip(_no_real_sleep):
    res = _check_broker_connect(None)
    assert res.severity == "soft" and res.ok is True
    assert "dry-run" in res.message


# ── runtime path (BrokerConnectTask) shares the same body ────────────────────


def test_runtime_task_recovers_on_bounded_retry(_no_real_sleep):
    broker = _FlakyBroker(fail_times=2)  # default max_attempts=3
    ctx = SimpleNamespace(broker=broker)
    res = BrokerConnectTask().check(ctx)
    assert res.name == "P-BROKER-CONNECT"
    assert res.severity == "hard" and res.ok is True
    assert "after 3 attempts" in res.message
    assert broker.attempts == 3


def test_runtime_task_fails_closed_when_outage_persists(_no_real_sleep):
    broker = _FlakyBroker(fail_times=999)
    ctx = SimpleNamespace(broker=broker)
    res = BrokerConnectTask().check(ctx)
    assert res.severity == "hard" and res.ok is False  # fail-closed
    assert "broker connect failed after 3 attempts" in res.message
    assert broker.attempts == 3


def test_both_entry_points_route_through_the_shared_body():
    # Identity guard against the pipeline twin-task trap: the runtime task must
    # call the shared retry body, not re-implement it.
    assert "_attempt_broker_connect" in BrokerConnectTask.check.__code__.co_names
    broker = _FlakyBroker(fail_times=0)
    direct = _attempt_broker_connect(broker)
    assert direct.ok is True and direct.name == "P-BROKER-CONNECT"
