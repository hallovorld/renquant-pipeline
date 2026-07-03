"""Stage-1 intraday decisioning on live state (RFC #208 §8 row 2).

Acceptance tests pinned by the RFC's pipeline row:

- SIM-PARITY (the headline): intraday-mode emits IDENTICAL decisions to
  batch-mode given identical (frozen-signal, snapshot-state) inputs.
- dedup vs the injected in-flight parent-intent set (idempotent emit).
- reserved-cash never negative; sizing against ``available`` (§7).
- envelope most-restrictive-wins (§10), entries only.
- exits-always-allowed (§10 A2 precedence).
- flag default-OFF byte-inertness (no evaluation, and nothing in the
  package imports the module).
- ``parent_intent_id`` byte-lockstep with slice 1 (execution #20), pinned
  by golden vectors generated from
  ``renquant_execution.order_state_machine.compute_parent_intent_id``.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from renquant_common import Task
from renquant_pipeline import InferenceContext, PanelScoringJob, RuntimeInferencePipeline, SelectionJob
from renquant_pipeline.intraday_decisioning import (
    DISABLED_REASON,
    FrozenDailySignal,
    IntradayEnvelopeLimits,
    IntradayLeakError,
    LiveStateSnapshot,
    ReservedCashError,
    SessionEnvelopeCounters,
    SessionStartSnapshot,
    compute_parent_intent_id,
    evaluate_entry_envelope,
    intraday_decisioning_enabled,
    run_intraday_decision_tick,
)

SRC_ROOT = Path(__file__).parent.parent / "src" / "renquant_pipeline"

TRADING_DAY = "2026-07-06"
ACCOUNT = "alpaca-live"
SIGNAL_VERSION = "sig-v1"
WATCHLIST = ["AAPL", "MSFT", "IBM"]
SCORES = {"AAPL": 0.7, "MSFT": 0.9, "IBM": 0.2}  # IBM below the 0.5 buy floor
QUANTITIES = {"AAPL": 4, "MSFT": 3, "IBM": 5}
PRICES = {"AAPL": 10.0, "MSFT": 20.0, "IBM": 5.0}


# ── fixture builders ─────────────────────────────────────────────────────────


def _strategy_config(*, intraday_enabled: bool | None = True) -> dict[str, Any]:
    config: dict[str, Any] = {
        "watchlist": list(WATCHLIST),
        "sector_map": {"AAPL": "TECH", "MSFT": "TECH", "IBM": "TECH"},
        "ranking": {
            "panel_scoring": {"enabled": True, "buy_floor": 0.5},
            "selection": {"enabled": True, "max_new_positions": 2},
        },
        "execution": {"default_quantity": 1},
    }
    if intraday_enabled is not None:
        config["intraday_decisioning"] = {"enabled": intraday_enabled}
    return config


def _data_manifest() -> dict[str, Any]:
    return {
        "dataset_id": "daily-fixture",
        "schema_version": "fixture-v1",
        "fingerprint": "sha256:data",
        "uri": "object://renquant-data/daily-fixture.parquet",
        "asset_class": "equity",
    }


def _artifact_manifest() -> dict[str, Any]:
    return {
        "artifact_id": "panel-ltr-prod",
        "model_family": "gbdt-panel-ltr",
        "strategy": "renquant_104",
        "fingerprint": "sha256:model",
        "uri": "object://renquant-artifacts/panel-ltr-prod.json",
        "promotion_status": "prod",
        "feature_cols": ["alpha_1"],
        "metrics": {"accepted": True},
    }


def _gate_inputs(quantities: dict[str, Any] | None = None) -> dict[str, Any]:
    """Class-B session-start snapshot content (frozen market gate inputs)."""
    return {
        "feature_frame": {ticker: {"alpha_1": 1.0} for ticker in WATCHLIST},
        "order_quantity_by_ticker": dict(quantities or QUANTITIES),
    }


def _signal(*, as_of: str = "2026-07-02") -> FrozenDailySignal:
    return FrozenDailySignal(signal_version=SIGNAL_VERSION, as_of=as_of, scores=dict(SCORES))


def _session_start(quantities: dict[str, Any] | None = None) -> SessionStartSnapshot:
    return SessionStartSnapshot.capture(
        _gate_inputs(quantities), captured_at=f"{TRADING_DAY}T13:35:00Z"
    )


def _live_state(
    *,
    cash: float = 10_000.0,
    equity: float = 100_000.0,
    open_buy_reservations: dict[str, float] | None = None,
    unsettled_buys: float = 0.0,
    prices: dict[str, float] | None = None,
) -> LiveStateSnapshot:
    return LiveStateSnapshot(
        as_of=f"{TRADING_DAY}T14:00:00Z",
        trading_day=TRADING_DAY,
        account=ACCOUNT,
        cash=cash,
        equity=equity,
        positions={},
        prices=dict(prices or PRICES),
        open_buy_reservations=dict(open_buy_reservations or {}),
        unsettled_buys=unsettled_buys,
    )


def _run_tick(**overrides: Any):
    kwargs: dict[str, Any] = dict(
        strategy_config=_strategy_config(),
        data_manifest=_data_manifest(),
        artifact_manifest=_artifact_manifest(),
        signal=_signal(),
        session_start=_session_start(),
        live_state=_live_state(),
    )
    kwargs.update(overrides)
    return run_intraday_decision_tick(**kwargs)


def _run_batch(
    *,
    strategy_config: dict[str, Any] | None = None,
    market_snapshot: dict[str, Any] | None = None,
    account_snapshot: dict[str, Any] | None = None,
) -> InferenceContext:
    """The batch-mode decision path on the SAME (signal, state) inputs.

    Mirrors the existing batch composition (see test_selection_contract):
    PanelScoringJob → SelectionJob → PanelScoringJob(emit_orders=True).
    """
    if market_snapshot is None:
        market_snapshot = dict(_gate_inputs())
        market_snapshot["as_of"] = TRADING_DAY
        market_snapshot["panel_scores"] = dict(SCORES)
        market_snapshot["signal_version"] = SIGNAL_VERSION
    ctx = InferenceContext(
        strategy_config=strategy_config or _strategy_config(),
        data_manifest=_data_manifest(),
        artifact_manifest=_artifact_manifest(),
        market_snapshot=market_snapshot,
        account_snapshot=account_snapshot or _live_state().to_account_snapshot(),
    )
    RuntimeInferencePipeline(
        [PanelScoringJob(), SelectionJob(), PanelScoringJob(emit_orders=True)]
    ).run(ctx)
    return ctx


class _BoomTask(Task):
    """Sentinel decision stage: the flag-off path must never run it."""

    def run(self, ctx: Any) -> bool | None:  # pragma: no cover - must not run
        raise AssertionError("decision stage ran while the intraday flag was OFF")


# ── flag default-OFF byte-inertness ──────────────────────────────────────────


def test_flag_defaults_off_and_disabled_tick_is_inert() -> None:
    assert intraday_decisioning_enabled({}) is False
    assert intraday_decisioning_enabled(_strategy_config(intraday_enabled=None)) is False
    assert intraday_decisioning_enabled(_strategy_config(intraday_enabled=False)) is False
    assert intraday_decisioning_enabled(_strategy_config()) is True

    # Flag absent → the tick returns disabled WITHOUT evaluating anything:
    # the sentinel stage would raise, and even a leak-violating signal is
    # not inspected (nothing runs, nothing is validated, nothing mutates).
    result = _run_tick(
        strategy_config=_strategy_config(intraday_enabled=None),
        signal=_signal(as_of=TRADING_DAY),  # would raise IntradayLeakError if inspected
        stages=[_BoomTask()],
    )
    assert result.enabled is False
    assert result.reason == DISABLED_REASON
    assert result.intents == ()
    assert result.skipped == ()
    assert result.blocked_by == {}
    assert result.decision_trace == ()


def test_no_pipeline_module_imports_intraday_decisioning() -> None:
    """Nothing wires into any live path: no module in the package (including
    ``__init__``) imports the intraday module — it is reachable only by an
    explicit external import from the orchestrator slice (§8 row 3)."""
    offenders: list[str] = []
    for py in sorted(SRC_ROOT.rglob("*.py")):
        if py.name == "intraday_decisioning.py":
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [alias.name for alias in node.names]
                if "intraday_decisioning" in module or "intraday_decisioning" in names:
                    offenders.append(str(py.relative_to(SRC_ROOT)))
            elif isinstance(node, ast.Import):
                if any("intraday_decisioning" in alias.name for alias in node.names):
                    offenders.append(str(py.relative_to(SRC_ROOT)))
    assert offenders == [], (
        f"intraday_decisioning must stay unwired (flag-gated, default-OFF) "
        f"but is imported by: {offenders}"
    )


# ── SIM-PARITY (§8 row 2 acceptance headline) ────────────────────────────────


def test_sim_parity_intraday_equals_batch_on_identical_inputs() -> None:
    batch_ctx = _run_batch()
    result = _run_tick()

    assert result.enabled is True

    batch_decisions = [
        (order["ticker"], order["action"], float(order["quantity"]))
        for order in batch_ctx.order_intents
    ]
    intraday_decisions = [
        (intent.symbol, intent.side.lower(), float(intent.quantity))
        for intent in result.intents
    ]
    assert batch_decisions == [("MSFT", "buy", 3.0), ("AAPL", "buy", 4.0)]
    assert intraday_decisions == batch_decisions

    # The attributed order payloads are byte-identical — the intraday path
    # is a new CALLER of the same gate stack, not a re-implementation.
    assert [dict(intent.order) for intent in result.intents] == batch_ctx.order_intents

    # Gate verdicts are identical too (same blocks for the same reasons).
    assert result.blocked_by == batch_ctx.blocked_by
    assert result.blocked_by == {"IBM": "panel_score_below_buy_floor"}

    # No intent was skipped: with a clean in-flight set, ample available
    # cash, and default envelope headroom the added intraday machinery is
    # decision-neutral.
    assert result.skipped == ()


def test_intraday_tick_is_deterministic_and_idempotent() -> None:
    first = _run_tick()
    second = _run_tick()
    assert first == second


# ── idempotent emit keyed on parent_intent_id ────────────────────────────────


def test_dedup_vs_in_flight_parent_intents() -> None:
    msft_buy = compute_parent_intent_id(
        account=ACCOUNT,
        symbol="MSFT",
        trading_day=TRADING_DAY,
        side="BUY",
        signal_version=SIGNAL_VERSION,
    )
    result = _run_tick(in_flight_parent_intents={msft_buy})

    assert [intent.symbol for intent in result.intents] == ["AAPL"]
    dup = [s for s in result.skipped if s.symbol == "MSFT"]
    assert len(dup) == 1
    assert dup[0].parent_intent_id == msft_buy
    assert dup[0].reasons == ("duplicate_parent_intent_in_flight",)


def test_at_most_one_intent_per_parent_within_a_tick() -> None:
    sell = {"ticker": "MSFT", "action": "sell", "quantity": 3}
    result = _run_tick(exit_orders=[sell, dict(sell)])

    exits = [i for i in result.intents if i.kind == "exit"]
    assert [i.symbol for i in exits] == ["MSFT"]
    dup = [s for s in result.skipped if s.reasons == ("duplicate_parent_intent_in_flight",)]
    assert len(dup) == 1
    parent_ids = [i.parent_intent_id for i in result.intents]
    assert len(parent_ids) == len(set(parent_ids))


def test_parent_intent_id_golden_vectors_lockstep_with_execution() -> None:
    """Golden vectors generated from slice 1's implementation
    (``renquant_execution.order_state_machine.compute_parent_intent_id``,
    execution repo main @ PR #20). If this test fails, the two repos'
    dedup keys have diverged — fix the drift, do not update the vectors
    unilaterally."""
    vectors = [
        (
            dict(account="alpaca-live", symbol="MSFT", trading_day="2026-07-06",
                 side="BUY", signal_version="sig-v1"),
            "pi-658b117032e9962b9831",
        ),
        (
            # case-insensitive on symbol + side, byte-equal digest
            dict(account="alpaca-live", symbol="msft", trading_day="2026-07-06",
                 side="buy", signal_version="sig-v1"),
            "pi-658b117032e9962b9831",
        ),
        (
            dict(account="alpaca-live", symbol="MSFT", trading_day="2026-07-06",
                 side="SELL", signal_version="sig-v1"),
            "pi-a0b7125fef710742aafd",
        ),
        (
            dict(account="alpaca-live", symbol="AAPL", trading_day="2026-07-06",
                 side="BUY", signal_version="sig-v1"),
            "pi-622ac483a1ce525e0c58",
        ),
        (
            dict(account="paper", symbol="IBM", trading_day="2026-07-07",
                 side="SELL", signal_version="panel-2026-07-06T2130Z"),
            "pi-add6207b065c5f973ddb",
        ),
    ]
    for kwargs, expected in vectors:
        assert compute_parent_intent_id(**kwargs) == expected


# ── §7 reserved-cash + sizing against available ──────────────────────────────


def test_reserved_cash_never_negative() -> None:
    with pytest.raises(ReservedCashError):
        _live_state(open_buy_reservations={"pi-x": -1.0})
    with pytest.raises(ReservedCashError):
        _live_state(unsettled_buys=-0.01)

    state = _live_state(
        cash=100.0, open_buy_reservations={"pi-a": 30.0, "pi-b": 10.0}, unsettled_buys=5.0
    )
    assert state.reserved_cash == pytest.approx(45.0)
    assert state.available_cash == pytest.approx(55.0)
    snapshot = state.to_account_snapshot()
    assert snapshot["cash"] == pytest.approx(100.0)
    assert snapshot["reserved_cash"] == pytest.approx(45.0)
    assert snapshot["available_cash"] == pytest.approx(55.0)


def test_sizing_uses_available_not_raw_cash() -> None:
    # cash 100, open-buy reservation 30 → available 70.
    # MSFT (3 × $20 = $60) fits (available → 10);
    # AAPL (4 × $10 = $40) does not — §7 caps it to the 1 whole share that
    # still fits. available_cash_end must never go negative.
    result = _run_tick(
        live_state=_live_state(cash=100.0, open_buy_reservations={"pi-open": 30.0}),
    )
    assert [(i.symbol, i.quantity) for i in result.intents] == [("MSFT", 3.0), ("AAPL", 1.0)]
    aapl = result.intents[1]
    assert aapl.resized_from == pytest.approx(4.0)
    assert result.available_cash_start == pytest.approx(70.0)
    assert result.available_cash_end == pytest.approx(0.0)
    assert result.available_cash_end >= 0.0

    # cash 65, nothing reserved → MSFT $60 fits, AAPL cannot afford one
    # share ($10 > $5 remaining) → blocked, never oversubscribed.
    result = _run_tick(live_state=_live_state(cash=65.0))
    assert [(i.symbol, i.quantity) for i in result.intents] == [("MSFT", 3.0)]
    blocked = [s for s in result.skipped if s.symbol == "AAPL"]
    assert blocked and blocked[0].reasons == ("insufficient_available_cash",)
    assert result.available_cash_end == pytest.approx(5.0)


def test_entries_size_in_whole_shares() -> None:
    result = _run_tick(session_start=_session_start({"AAPL": 4, "MSFT": 2.7, "IBM": 5}))
    msft = [i for i in result.intents if i.symbol == "MSFT"]
    assert msft and msft[0].quantity == pytest.approx(2.0)


# ── §10 envelope: most-restrictive-wins, entries only ────────────────────────


def test_envelope_most_restrictive_wins_unit() -> None:
    limits = IntradayEnvelopeLimits(
        max_new_entries_per_day=3,
        max_deployment_fraction=0.15,
        max_turnover_fraction=0.25,
    )
    base = dict(limits=limits, equity=100.0, entry_notional=10.0, reserved_cash=0.0)

    ok = evaluate_entry_envelope(counters=SessionEnvelopeCounters(), **base)
    assert ok.allowed is True and ok.reasons == ()

    # each constraint alone blocks (any single violation is enough) …
    entries = evaluate_entry_envelope(
        counters=SessionEnvelopeCounters(entries_count=3), **base
    )
    assert entries.allowed is False and entries.reasons == ("max_new_entries_per_day",)

    deployment = evaluate_entry_envelope(
        counters=SessionEnvelopeCounters(deployed_notional=6.0), **base
    )
    assert deployment.allowed is False and deployment.reasons == ("max_deployment_fraction",)

    turnover = evaluate_entry_envelope(
        counters=SessionEnvelopeCounters(turnover_notional=16.0), **base
    )
    assert turnover.allowed is False and turnover.reasons == ("max_turnover_fraction",)

    # … and simultaneous violations are ALL reported (not just the first).
    all_blocked = evaluate_entry_envelope(
        counters=SessionEnvelopeCounters(
            entries_count=3, deployed_notional=6.0, turnover_notional=16.0
        ),
        **base,
    )
    assert all_blocked.allowed is False
    assert all_blocked.reasons == (
        "max_new_entries_per_day",
        "max_deployment_fraction",
        "max_turnover_fraction",
    )


def test_envelope_consumes_injected_headroom_evaluator() -> None:
    """Slice 1's A2 evaluator is consumed via injection — its structural
    ``EntryDecision`` (allowed/reason) shape is honored, and it is invoked
    with the §7 reserved-cash figure, never reimplemented here."""
    calls: list[dict[str, float]] = []

    class _Decision:
        allowed = False
        reason = "insufficient_buying_power_headroom"

    def evaluator(*, entry_notional: float, reserved_cash: float) -> Any:
        calls.append({"entry_notional": entry_notional, "reserved_cash": reserved_cash})
        return _Decision()

    verdict = evaluate_entry_envelope(
        limits=IntradayEnvelopeLimits(),
        counters=SessionEnvelopeCounters(),
        equity=100_000.0,
        entry_notional=60.0,
        reserved_cash=30.0,
        headroom_evaluator=evaluator,
    )
    assert verdict.allowed is False
    assert verdict.reasons == ("insufficient_buying_power_headroom",)
    assert calls == [{"entry_notional": 60.0, "reserved_cash": 30.0}]


def test_envelope_blocks_entries_through_the_tick() -> None:
    result = _run_tick(session_counters=SessionEnvelopeCounters(entries_count=3))
    assert [i for i in result.intents if i.kind == "entry"] == []
    entry_skips = {s.symbol: s.reasons for s in result.skipped}
    assert entry_skips["MSFT"] == ("max_new_entries_per_day",)
    assert entry_skips["AAPL"] == ("max_new_entries_per_day",)
    # the gate stack itself still admitted them — the envelope is a §10
    # emission constraint, not a new gate.
    assert result.blocked_by == {"IBM": "panel_score_below_buy_floor"}


def test_tick_counters_accumulate_pending_and_sells_correctly() -> None:
    # deployment counts buys (incl this tick's emissions); sells consume
    # turnover but never deployment.
    result = _run_tick(exit_orders=[{"ticker": "IBM", "action": "sell", "quantity": 2}])
    # exits first: IBM 2 × $5 = $10 turnover, no deployment.
    # entries: MSFT $60 + AAPL $40.
    assert result.counters.entries_count == 2
    assert result.counters.deployed_notional == pytest.approx(100.0)
    assert result.counters.turnover_notional == pytest.approx(110.0)


# ── §10 exits-always-allowed precedence ──────────────────────────────────────


def test_exits_always_allowed_bypass_envelope_and_headroom() -> None:
    def forbidden_evaluator(*, entry_notional: float, reserved_cash: float) -> Any:
        raise AssertionError(
            "the buying-power headroom evaluator must never run for exits"
        )

    # Everything that could bind an entry is exhausted: zero available cash
    # (cash fully reserved), every envelope counter breached. The protective
    # exit still goes out; the entries are refused.
    result = _run_tick(
        live_state=_live_state(cash=100.0, open_buy_reservations={"pi-open": 100.0}),
        session_counters=SessionEnvelopeCounters(
            entries_count=99, deployed_notional=1e9, turnover_notional=1e9
        ),
        headroom_evaluator=forbidden_evaluator,
        exit_orders=[{"ticker": "MSFT", "action": "sell", "quantity": 3}],
    )
    exits = [i for i in result.intents if i.kind == "exit"]
    assert [(i.symbol, i.side, i.quantity) for i in exits] == [("MSFT", "SELL", 3.0)]
    assert [i for i in result.intents if i.kind == "entry"] == []
    for skip in result.skipped:
        assert skip.side == "BUY"  # only entries were refused


def test_exit_emitted_even_without_reference_price() -> None:
    # §11b: exits favor action over quote freshness — a missing class-D
    # price zeroes the turnover accounting but never blocks the exit.
    result = _run_tick(
        live_state=_live_state(prices={"AAPL": 10.0, "MSFT": 20.0}),  # no IBM quote
        exit_orders=[{"ticker": "IBM", "action": "sell", "quantity": 2}],
    )
    exits = [i for i in result.intents if i.kind == "exit"]
    assert [(i.symbol, i.notional) for i in exits] == [("IBM", 0.0)]

    # …while an ENTRY without a reference price fails closed.
    result = _run_tick(live_state=_live_state(prices={"MSFT": 20.0}))
    assert [i.symbol for i in result.intents] == ["MSFT"]
    blocked = [s for s in result.skipped if s.symbol == "AAPL"]
    assert blocked and blocked[0].reasons == ("missing_reference_price",)


# ── §6 four-class point-in-time guards ───────────────────────────────────────


def test_class_a_signal_must_predate_the_session() -> None:
    with pytest.raises(IntradayLeakError):
        _run_tick(signal=_signal(as_of=TRADING_DAY))
    with pytest.raises(IntradayLeakError):
        _run_tick(signal=_signal(as_of="2026-07-07"))


def test_class_b_session_start_snapshot_is_frozen() -> None:
    snapshot = _session_start()
    snapshot.verify()  # clean round-trip

    tampered = replace(
        snapshot,
        gate_inputs={**dict(snapshot.gate_inputs), "order_quantity_by_ticker": {"MSFT": 99}},
    )
    with pytest.raises(IntradayLeakError):
        tampered.verify()
    with pytest.raises(IntradayLeakError):
        _run_tick(session_start=tampered)
