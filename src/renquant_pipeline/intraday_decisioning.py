"""Stage-1 intraday decisioning on LIVE state (RFC #208 §8 row 2).

Pipeline slice of the renquant105 Stage-1 build (orchestrator
``doc/design/2026-06-30-renquant105-intraday-decisioning-architecture.md``):
a NEW CALLER of the EXISTING runtime gate stack that evaluates decisions
against a live-state snapshot instead of the batch context, then emits
idempotent order INTENTS (never orders). Nothing here wires into any live
path: the feature is config-flag gated, default OFF
(``strategy_config["intraday_decisioning"]["enabled"]``), and no other
pipeline module imports this one (pinned by a regression test). The global
env kill switch + canary allowlist (§10) belong to the orchestrator slice
(§8 row 3), not here.

Four-class input contract (§6) — enforced structurally by the context
builder, which sources each field from exactly one class:

- **Class A (frozen signal)**: :class:`FrozenDailySignal` — the T-1 EOD
  scores vector + ``signal_version``. Must predate the session
  (:meth:`FrozenDailySignal.assert_predates_session`), never re-scored here.
- **Class B (session-start PIT gate inputs)**: :class:`SessionStartSnapshot`
  — gate-stack market inputs snapshotted + fingerprinted at the first
  eligible tick, verified frozen on every subsequent tick.
- **Class C (live state)**: :class:`LiveStateSnapshot` — positions / cash /
  pending reservations, changes every tick by design.
- **Class D (timing-only quote)**: ``LiveStateSnapshot.prices`` is used ONLY
  for sizing/notional reservation of already-admitted decisions; it never
  feeds a model or gate input.

Contracts consumed from slice 1 (renquant-execution #20,
``renquant_execution.order_state_machine``) — consumed, not reimplemented.
The pipeline may not import renquant-execution (execution consumes pipeline
intents, not the reverse), so the seams are:

- ``compute_parent_intent_id`` is kept in BYTE-LOCKSTEP with the execution
  implementation (same sha256-over-unit-separator recipe) and pinned by
  golden vectors generated from the execution module — see
  ``tests/test_intraday_decisioning.py``. One shared implementation in
  renquant-common is the follow-up (same lesson as the model_content_sha256
  triple-impl unification).
- The A2 buying-power headroom evaluator
  (``order_state_machine.evaluate_entry_headroom``) is INJECTED via the
  :class:`HeadroomEvaluator` protocol; its ``EntryDecision`` result is read
  structurally (``.allowed`` / ``.reason``).
- Reserved-cash accounting mirrors ``OrderStateBook.reserved_cash``:
  ``reserved = Σ open-buy-child reservations + unsettled buys`` and sizing
  uses ``available = cash − reserved`` (§7), never raw broker cash.

Sim-parity (§8 row 2 acceptance): the intraday entry point runs the SAME
stage composition batch-mode runs (``default_decision_stages()``), so given
identical (frozen-signal, snapshot-state) inputs the decision set is
identical BY CONSTRUCTION; the harness in the tests proves it by running
both paths on one fixture and asserting decision-set equality.

Envelope interaction rules (§10): most-restrictive-wins over
{entries-count, deployment-notional, turnover, injected buying-power
headroom} — every violated constraint is reported, any one blocks; all
constraints bind ENTRIES ONLY; exits are never routed through the envelope
(exits-always-allowed precedence).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from typing import Any, Collection, Iterable, Mapping, Protocol, Sequence

from renquant_artifacts import hash_jsonable
from renquant_common import Task

from .inference import InferenceContext, RuntimeInferencePipeline
from .panel_scoring import FrozenScoreScoringJob, PanelScoringJob
from .selection import SelectionJob

INTRADAY_DECISIONING_SCHEMA_VERSION = "intraday-decisioning-v1"

#: Config flag (default OFF). The orchestrator slice owns the global env
#: kill switch + canary allowlist (§10); this flag only arms the pipeline
#: entry point itself.
INTRADAY_FLAG_SECTION = "intraday_decisioning"
INTRADAY_FLAG_KEY = "enabled"

DISABLED_REASON = "intraday_decisioning_disabled"

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

_QTY_EPS = 1e-9
_FIELD_SEP = "\x1f"  # unit separator — lockstep with order_state_machine


class IntradayContractError(ValueError):
    """A Stage-1 intraday input violated its declared contract."""


class IntradayLeakError(IntradayContractError):
    """A class-A/B input violated the §6 point-in-time contract."""


class ReservedCashError(IntradayContractError):
    """Reserved-cash accounting would go negative (§7 invariant)."""


def compute_parent_intent_id(
    *,
    account: str,
    symbol: str,
    trading_day: str,
    side: str,
    signal_version: str,
) -> str:
    """Deterministic dedup key for one *decision* (§7 two-level id).

    BYTE-LOCKSTEP contract with
    ``renquant_execution.order_state_machine.compute_parent_intent_id`` —
    both sides hash the same unit-separator payload with sha256 and take
    ``"pi-" + digest[:20]``. Golden vectors generated from the execution
    module pin the equality in ``tests/test_intraday_decisioning.py``; any
    change must land in both repos (or better: move the one implementation
    to renquant-common).
    """
    payload = _FIELD_SEP.join(
        [
            str(account),
            str(symbol).upper(),
            str(trading_day),
            str(side).upper(),
            str(signal_version),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"pi-{digest[:20]}"


# ---------------------------------------------------------------------------
# §6 input classes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrozenDailySignal:
    """Class A: the T-1 EOD conviction scores, frozen for the session."""

    signal_version: str
    as_of: str  # ISO date of the EOD build the scores came from
    scores: Mapping[str, float]

    def __post_init__(self) -> None:
        if not str(self.signal_version):
            raise IntradayContractError("FrozenDailySignal.signal_version is required")
        if not str(self.as_of):
            raise IntradayContractError("FrozenDailySignal.as_of is required")
        for ticker, score in dict(self.scores).items():
            if not _is_finite(score):
                raise IntradayContractError(
                    f"FrozenDailySignal score for {ticker!r} is not finite: {score!r}"
                )

    def assert_predates_session(self, trading_day: str) -> None:
        """§6 leak guard: class A must be built strictly BEFORE the session.

        ISO dates compare lexicographically, so no calendar dependency is
        needed for the hard no-look-ahead bound. Calendar staleness policy
        (missing / older-than-one-trading-day → sell-only fallback) is the
        orchestrator slice's session policy (§8 row 3).
        """
        if not str(trading_day):
            raise IntradayContractError("trading_day is required")
        if str(self.as_of) >= str(trading_day):
            raise IntradayLeakError(
                f"class-A signal as_of {self.as_of!r} does not predate the "
                f"session {trading_day!r}; the intraday path must never act "
                "on a same-day or future signal build (§6)"
            )


@dataclass(frozen=True)
class SessionStartSnapshot:
    """Class B: session-start PIT gate inputs, fingerprinted then frozen.

    ``gate_inputs`` carries the market-snapshot fields the gate stack reads
    (feature_frame, order quantities, regime evidence, …). It is captured
    once at the first eligible tick; :meth:`verify` re-fingerprints on every
    subsequent tick so a mid-session mutation is a hard failure, not a
    silent drift (§6 replay obligation, class-B leg).
    """

    captured_at: str
    gate_inputs: Mapping[str, Any]
    gate_input_fingerprint: str

    @classmethod
    def capture(
        cls, gate_inputs: Mapping[str, Any], *, captured_at: str
    ) -> "SessionStartSnapshot":
        inputs = dict(gate_inputs)
        return cls(
            captured_at=str(captured_at),
            gate_inputs=inputs,
            gate_input_fingerprint=hash_jsonable(inputs),
        )

    def verify(self) -> None:
        actual = hash_jsonable(dict(self.gate_inputs))
        if actual != self.gate_input_fingerprint:
            raise IntradayLeakError(
                "class-B session-start gate inputs mutated after capture: "
                f"fingerprint {self.gate_input_fingerprint!r} != {actual!r} (§6)"
            )


@dataclass(frozen=True)
class LiveStateSnapshot:
    """Class C: broker-truth state at tick start — changes every tick.

    ``open_buy_reservations`` is keyed on ``parent_intent_id`` and carries
    the §7 reservation of each OPEN buy child (``unfilled_qty × price``) —
    the same accounting ``OrderStateBook.reserved_cash`` produces in slice 1.
    """

    as_of: str
    trading_day: str
    account: str
    cash: float
    equity: float
    positions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    prices: Mapping[str, float] = field(default_factory=dict)  # class D only
    open_buy_reservations: Mapping[str, float] = field(default_factory=dict)
    unsettled_buys: float = 0.0
    pending_broker_tickers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("as_of", "trading_day", "account"):
            if not str(getattr(self, name)):
                raise IntradayContractError(f"LiveStateSnapshot.{name} is required")
        if not _is_finite(self.cash):
            raise IntradayContractError(f"cash is not finite: {self.cash!r}")
        if not _is_finite(self.equity):
            raise IntradayContractError(f"equity is not finite: {self.equity!r}")
        if self.unsettled_buys < 0 or not _is_finite(self.unsettled_buys):
            raise ReservedCashError(
                f"unsettled_buys must be >= 0 and finite: {self.unsettled_buys!r}"
            )
        for parent_id, reservation in dict(self.open_buy_reservations).items():
            if not _is_finite(reservation) or float(reservation) < 0:
                raise ReservedCashError(
                    f"open-buy reservation for {parent_id!r} must be >= 0 "
                    f"and finite: {reservation!r} (§7: reserved_cash can "
                    "never go negative)"
                )

    @property
    def reserved_cash(self) -> float:
        """§7: Σ open-buy-child reservations + unsettled buys (>= 0)."""
        return (
            sum(float(v) for v in dict(self.open_buy_reservations).values())
            + float(self.unsettled_buys)
        )

    @property
    def available_cash(self) -> float:
        """§7 sizing basis: ``cash − reserved_cash`` — never raw broker cash."""
        return float(self.cash) - self.reserved_cash

    def to_account_snapshot(self) -> dict[str, Any]:
        """Runtime ``account_snapshot`` shape (positions/cash/portfolio_value)."""
        positions: dict[str, dict[str, Any]] = {}
        for raw_ticker, row in dict(self.positions).items():
            ticker = str(raw_ticker)
            normalized = dict(row)
            normalized.setdefault("ticker", ticker)
            positions[ticker] = normalized
        return {
            "positions": positions,
            "cash": float(self.cash),
            "portfolio_value": float(self.equity),
            "reserved_cash": self.reserved_cash,
            "available_cash": self.available_cash,
        }


# ---------------------------------------------------------------------------
# §10 entry envelope — most-restrictive-wins; binds ENTRIES only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntradayEnvelopeLimits:
    """§10 conservative Stage-1 defaults (sized for the ~$10.5k book)."""

    max_new_entries_per_day: int = 3
    max_deployment_fraction: float = 0.15  # of equity, incl pending buys
    max_turnover_fraction: float = 0.25  # of equity, gross buys + sells


@dataclass(frozen=True)
class SessionEnvelopeCounters:
    """Session-cumulative §10 counters; the caller carries them across ticks.

    ``deployed_notional`` counts net new long notional INCLUDING open/pending
    buy children (a pending buy already consumes deployment headroom);
    ``turnover_notional`` counts gross buys AND sells (sells consume turnover
    but never deployment).
    """

    entries_count: int = 0
    deployed_notional: float = 0.0
    turnover_notional: float = 0.0


class HeadroomEvaluator(Protocol):
    """Injection seam for slice 1's A2 buying-power headroom evaluator.

    The orchestrator binds ``renquant_execution.order_state_machine
    .evaluate_entry_headroom`` (with its envelope + recorded broker-regime
    snapshot) to this shape; the result is read structurally (``allowed`` /
    ``reason``) so the pipeline consumes — never reimplements — the
    evaluator. It must NEVER be called for exits.
    """

    def __call__(self, *, entry_notional: float, reserved_cash: float) -> Any: ...


@dataclass(frozen=True)
class EnvelopeVerdict:
    allowed: bool
    reasons: tuple[str, ...]  # every violated constraint (audit surface)


def evaluate_entry_envelope(
    *,
    limits: IntradayEnvelopeLimits,
    counters: SessionEnvelopeCounters,
    equity: float,
    entry_notional: float,
    reserved_cash: float,
    headroom_evaluator: HeadroomEvaluator | None = None,
) -> EnvelopeVerdict:
    """§10 interaction rule: most-restrictive-wins, entries only.

    An entry is blocked the moment ANY of {entries-count, deployment
    notional, turnover, injected buying-power headroom} would be exceeded;
    the constraints are not additive. All violated reasons are returned so
    the decision ledger records the full set, not just the first.
    Exits must never be routed through this function.
    """
    reasons: list[str] = []
    if counters.entries_count + 1 > limits.max_new_entries_per_day:
        reasons.append("max_new_entries_per_day")
    if equity <= 0 or not _is_finite(equity):
        reasons.append("nonpositive_equity")
    else:
        if (
            counters.deployed_notional + entry_notional
            > limits.max_deployment_fraction * equity + _QTY_EPS
        ):
            reasons.append("max_deployment_fraction")
        if (
            counters.turnover_notional + entry_notional
            > limits.max_turnover_fraction * equity + _QTY_EPS
        ):
            reasons.append("max_turnover_fraction")
    if headroom_evaluator is not None:
        decision = headroom_evaluator(
            entry_notional=float(entry_notional),
            reserved_cash=float(reserved_cash),
        )
        if not bool(getattr(decision, "allowed", False)):
            reasons.append(
                str(getattr(decision, "reason", "buying_power_headroom_blocked"))
            )
    return EnvelopeVerdict(allowed=not reasons, reasons=tuple(reasons))


# ---------------------------------------------------------------------------
# Intent emission — idempotent, keyed on parent_intent_id.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    """One emitted decision INTENT (never an order; execution owns orders)."""

    parent_intent_id: str
    account: str
    symbol: str
    side: str  # BUY | SELL
    kind: str  # "entry" | "exit"
    quantity: float
    price: float | None  # class-D reference used for sizing; None for exits
    notional: float
    trading_day: str
    signal_version: str
    order: Mapping[str, Any]  # the attributed order payload (audit surface)
    resized_from: float | None = None  # §7 available-cash cap, when applied


@dataclass(frozen=True)
class SkippedIntent:
    """A decision that was NOT emitted, with its audit reason(s)."""

    symbol: str
    side: str
    parent_intent_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class IntradayTickResult:
    """Deterministic output of one intraday decision tick."""

    schema_version: str
    enabled: bool
    reason: str
    trading_day: str = ""
    signal_version: str = ""
    gate_input_fingerprint: str = ""
    intents: tuple[OrderIntent, ...] = ()
    skipped: tuple[SkippedIntent, ...] = ()
    blocked_by: Mapping[str, str] = field(default_factory=dict)
    decision_trace: tuple[Mapping[str, Any], ...] = ()
    counters: SessionEnvelopeCounters = field(default_factory=SessionEnvelopeCounters)
    reserved_cash: float = 0.0
    available_cash_start: float = 0.0
    available_cash_end: float = 0.0


def intraday_decisioning_enabled(strategy_config: Mapping[str, Any]) -> bool:
    """Default-OFF flag read; absent config section means disabled."""
    section = (strategy_config or {}).get(INTRADAY_FLAG_SECTION) or {}
    if not isinstance(section, Mapping):
        return False
    return bool(section.get(INTRADAY_FLAG_KEY, False))


def default_decision_stages() -> list[Task]:
    """The SAME stage composition batch-mode runs — sim-parity by construction.

    PanelScoringJob (gate stack: artifact contract → feature contract →
    scores → calibration → model admission → weak-buy veto) → SelectionJob
    (top-k selection that cannot promote blocked names) → attributed order
    emit. A new gate added to the batch composition automatically binds
    intraday because both paths call this one function.
    """
    return [PanelScoringJob(), SelectionJob(), PanelScoringJob(emit_orders=True)]


def build_intraday_context(
    *,
    strategy_config: Mapping[str, Any],
    data_manifest: Mapping[str, Any],
    artifact_manifest: Mapping[str, Any],
    signal: FrozenDailySignal,
    session_start: SessionStartSnapshot,
    live_state: LiveStateSnapshot,
) -> InferenceContext:
    """Assemble the runtime InferenceContext from the four §6 input classes.

    Every market-snapshot field comes from class B (session-start snapshot),
    the scores vector comes from class A (frozen signal — authoritative, it
    OVERWRITES any panel_scores a caller left in the class-B inputs), and
    the account snapshot comes from class C (live state). Class D prices are
    NOT placed in the market snapshot — they are used only downstream for
    intent sizing, so no gate or scorer can read them.
    """
    market: dict[str, Any] = dict(session_start.gate_inputs)
    market["as_of"] = str(live_state.trading_day)
    market["panel_scores"] = dict(signal.scores)
    market["signal_version"] = str(signal.signal_version)
    return InferenceContext(
        strategy_config=dict(strategy_config),
        data_manifest=dict(data_manifest),
        artifact_manifest=dict(artifact_manifest),
        market_snapshot=market,
        account_snapshot=live_state.to_account_snapshot(),
    )


def run_intraday_decision_tick(
    *,
    strategy_config: Mapping[str, Any],
    data_manifest: Mapping[str, Any],
    artifact_manifest: Mapping[str, Any],
    signal: FrozenDailySignal,
    session_start: SessionStartSnapshot,
    live_state: LiveStateSnapshot,
    in_flight_parent_intents: Collection[str] = (),
    exit_orders: Sequence[Mapping[str, Any]] = (),
    envelope_limits: IntradayEnvelopeLimits | None = None,
    session_counters: SessionEnvelopeCounters | None = None,
    headroom_evaluator: HeadroomEvaluator | None = None,
    stages: Sequence[Task] | None = None,
) -> IntradayTickResult:
    """One intraday decision tick: gate stack on live state → idempotent intents.

    Pure in-memory: no broker I/O, no persistence, read-only over every
    input. Deterministic — identical inputs produce an identical result, so
    a double-fired tick emits the identical intent set and the
    ``parent_intent_id`` dedup (here and in slice 1's OrderStateBook) makes
    the duplicate harmless.

    ``exit_orders`` are protective-exit order payloads produced by the
    existing risk/sell path (e.g. the sell-only loop). Per §10
    exits-always-allowed they bypass the envelope, the entry halt, and the
    headroom evaluator unconditionally — they are only deduplicated.

    When the flag is OFF (default) the function returns a disabled result
    WITHOUT evaluating anything — no gate runs, no context is built, no
    input contract is enforced (byte-inert).
    """
    if not intraday_decisioning_enabled(strategy_config):
        return IntradayTickResult(
            schema_version=INTRADAY_DECISIONING_SCHEMA_VERSION,
            enabled=False,
            reason=DISABLED_REASON,
        )

    # §6 point-in-time guards: class A predates the session; class B frozen.
    signal.assert_predates_session(live_state.trading_day)
    session_start.verify()

    ctx = build_intraday_context(
        strategy_config=strategy_config,
        data_manifest=data_manifest,
        artifact_manifest=artifact_manifest,
        signal=signal,
        session_start=session_start,
        live_state=live_state,
    )
    RuntimeInferencePipeline(list(stages) if stages is not None else default_decision_stages()).run(ctx)

    return _emit_intents(
        ctx=ctx,
        signal=signal,
        session_start=session_start,
        live_state=live_state,
        in_flight_parent_intents=in_flight_parent_intents,
        exit_orders=exit_orders,
        limits=envelope_limits or IntradayEnvelopeLimits(),
        counters=session_counters or SessionEnvelopeCounters(),
        headroom_evaluator=headroom_evaluator,
    )


def frozen_score_diagnostic_stages() -> list[Task]:
    """Stage list that uses pre-computed frozen scores instead of rebuilding
    features from scratch each tick — see :func:`run_frozen_score_diagnostic_tick`
    for the scope this is confined to."""
    return [
        FrozenScoreScoringJob(),
        SelectionJob(),
        FrozenScoreScoringJob(emit_orders=True),
    ]


def run_frozen_score_diagnostic_tick(
    *,
    strategy_config: Mapping[str, Any],
    data_manifest: Mapping[str, Any],
    artifact_manifest: Mapping[str, Any],
    signal: FrozenDailySignal,
    session_start: SessionStartSnapshot,
    live_state: LiveStateSnapshot,
    in_flight_parent_intents: Collection[str] = (),
    exit_orders: Sequence[Mapping[str, Any]] = (),
    envelope_limits: IntradayEnvelopeLimits | None = None,
    session_counters: SessionEnvelopeCounters | None = None,
    headroom_evaluator: HeadroomEvaluator | None = None,
) -> IntradayTickResult:
    """Slice-2 contract entry point for the frozen-score diagnostic probe.

    DIAGNOSTIC / DEBUG PROBE ONLY — not a validated intent-generation
    design (see :class:`~renquant_pipeline.panel_scoring.FrozenScoreScoringJob`
    for the exact semantic-validity caveats: an empty feature matrix and a
    hardcoded ``default_quantity=1`` unblock the per-tick feature gate, but
    there is no proof this preserves the pipeline's real semantics, no
    sizing control, and no exit/sell path). Exists so a caller that only
    has a frozen daily signal (no live feature build) can still exercise
    the gate stack end-to-end for debugging, WITHOUT reaching into
    ``panel_scoring``/``selection`` internals to compose its own stage
    graph — that composition belongs here, behind this contract, not in a
    consuming repo (RFC #208 §8 row 2/3 boundary).

    Otherwise identical to :func:`run_intraday_decision_tick` — same input
    classes, same envelope/idempotency semantics, same output shape.
    """
    return run_intraday_decision_tick(
        strategy_config=strategy_config,
        data_manifest=data_manifest,
        artifact_manifest=artifact_manifest,
        signal=signal,
        session_start=session_start,
        live_state=live_state,
        in_flight_parent_intents=in_flight_parent_intents,
        exit_orders=exit_orders,
        envelope_limits=envelope_limits,
        session_counters=session_counters,
        headroom_evaluator=headroom_evaluator,
        stages=frozen_score_diagnostic_stages(),
    )


def _emit_intents(
    *,
    ctx: InferenceContext,
    signal: FrozenDailySignal,
    session_start: SessionStartSnapshot,
    live_state: LiveStateSnapshot,
    in_flight_parent_intents: Collection[str],
    exit_orders: Sequence[Mapping[str, Any]],
    limits: IntradayEnvelopeLimits,
    counters: SessionEnvelopeCounters,
    headroom_evaluator: HeadroomEvaluator | None,
) -> IntradayTickResult:
    intents: list[OrderIntent] = []
    skipped: list[SkippedIntent] = []
    seen_parents: set[str] = {str(pid) for pid in in_flight_parent_intents}
    available_start = live_state.available_cash
    available = available_start
    # Running §7 reservation: broker-known reservations + entries accepted
    # THIS tick, so overlapping intents within one tick cannot spend the
    # same dollar either.
    running_reserved = live_state.reserved_cash

    def parent_id(symbol: str, side: str) -> str:
        return compute_parent_intent_id(
            account=live_state.account,
            symbol=symbol,
            trading_day=live_state.trading_day,
            side=side,
            signal_version=signal.signal_version,
        )

    # -- exits first: §10 exits-always-allowed. Never routed through the
    # envelope / headroom evaluator; only idempotency applies.
    for order in exit_orders:
        symbol = _order_symbol(order)
        if not symbol:
            skipped.append(
                SkippedIntent(
                    symbol=str(order.get("ticker") or order.get("symbol") or ""),
                    side=SIDE_SELL,
                    parent_intent_id="",
                    reasons=("malformed_exit_order_missing_symbol",),
                )
            )
            continue
        pid = parent_id(symbol, SIDE_SELL)
        if pid in seen_parents:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_SELL,
                    parent_intent_id=pid,
                    reasons=("duplicate_parent_intent_in_flight",),
                )
            )
            continue
        qty = _positive_quantity(order)
        if qty is None:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_SELL,
                    parent_intent_id=pid,
                    reasons=("malformed_exit_order_missing_quantity",),
                )
            )
            continue
        price = _reference_price(live_state, symbol)
        # §11b: exits favor action over quote freshness — a missing
        # reference price only zeroes the turnover accounting, it never
        # blocks the exit.
        notional = qty * price if price is not None else 0.0
        counters = replace(
            counters,
            turnover_notional=counters.turnover_notional + notional,
        )
        seen_parents.add(pid)
        intents.append(
            OrderIntent(
                parent_intent_id=pid,
                account=live_state.account,
                symbol=symbol,
                side=SIDE_SELL,
                kind="exit",
                quantity=qty,
                price=price,
                notional=notional,
                trading_day=live_state.trading_day,
                signal_version=signal.signal_version,
                order=dict(order),
            )
        )

    # -- entries: gate-stack output, sized against `available`, enveloped.
    for order in ctx.order_intents:
        symbol = _order_symbol(order)
        action = str(order.get("action") or "").lower()
        if not symbol or action != "buy":
            skipped.append(
                SkippedIntent(
                    symbol=symbol or "",
                    side=str(order.get("action") or "?").upper(),
                    parent_intent_id="",
                    reasons=("unsupported_entry_order",),
                )
            )
            continue
        pid = parent_id(symbol, SIDE_BUY)
        if pid in seen_parents:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_BUY,
                    parent_intent_id=pid,
                    reasons=("duplicate_parent_intent_in_flight",),
                )
            )
            continue
        raw_qty = _positive_quantity(order)
        if raw_qty is None:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_BUY,
                    parent_intent_id=pid,
                    reasons=("malformed_entry_order_missing_quantity",),
                )
            )
            continue
        price = _reference_price(live_state, symbol)
        if price is None:
            # Entries fail closed without a sizing reference (class D).
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_BUY,
                    parent_intent_id=pid,
                    reasons=("missing_reference_price",),
                )
            )
            continue
        # Stage 1 sizes in WHOLE shares (§2: fractional is a Stage-2
        # dependency, explicitly not assumed here).
        qty = float(math.floor(raw_qty))
        resized_from: float | None = None
        if qty < 1.0:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_BUY,
                    parent_intent_id=pid,
                    reasons=("zero_quantity_after_whole_share_floor",),
                )
            )
            continue
        notional = qty * price
        if notional > available + _QTY_EPS:
            # §7: sizing uses available = cash − reserved. Cap to the whole
            # shares that still fit; block when none do.
            capped = float(math.floor(max(available, 0.0) / price))
            if capped < 1.0:
                skipped.append(
                    SkippedIntent(
                        symbol=symbol,
                        side=SIDE_BUY,
                        parent_intent_id=pid,
                        reasons=("insufficient_available_cash",),
                    )
                )
                continue
            resized_from = qty
            qty = capped
            notional = qty * price
        verdict = evaluate_entry_envelope(
            limits=limits,
            counters=counters,
            equity=live_state.equity,
            entry_notional=notional,
            reserved_cash=running_reserved,
            headroom_evaluator=headroom_evaluator,
        )
        if not verdict.allowed:
            skipped.append(
                SkippedIntent(
                    symbol=symbol,
                    side=SIDE_BUY,
                    parent_intent_id=pid,
                    reasons=verdict.reasons,
                )
            )
            continue
        counters = replace(
            counters,
            entries_count=counters.entries_count + 1,
            deployed_notional=counters.deployed_notional + notional,
            turnover_notional=counters.turnover_notional + notional,
        )
        available -= notional
        running_reserved += notional
        seen_parents.add(pid)
        intents.append(
            OrderIntent(
                parent_intent_id=pid,
                account=live_state.account,
                symbol=symbol,
                side=SIDE_BUY,
                kind="entry",
                quantity=qty,
                price=price,
                notional=notional,
                trading_day=live_state.trading_day,
                signal_version=signal.signal_version,
                order=dict(order),
                resized_from=resized_from,
            )
        )

    return IntradayTickResult(
        schema_version=INTRADAY_DECISIONING_SCHEMA_VERSION,
        enabled=True,
        reason="ok",
        trading_day=live_state.trading_day,
        signal_version=signal.signal_version,
        gate_input_fingerprint=session_start.gate_input_fingerprint,
        intents=tuple(intents),
        skipped=tuple(skipped),
        blocked_by=dict(getattr(ctx, "blocked_by", {}) or {}),
        decision_trace=tuple(dict(row) for row in (ctx.decision_trace or [])),
        counters=counters,
        reserved_cash=live_state.reserved_cash,
        available_cash_start=available_start,
        available_cash_end=available,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _order_symbol(order: Mapping[str, Any]) -> str:
    value = order.get("ticker") or order.get("symbol")
    return str(value).upper() if value else ""


def _positive_quantity(order: Mapping[str, Any]) -> float | None:
    value = order.get("quantity", order.get("qty"))
    try:
        qty = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(qty) or qty <= 0:
        return None
    return qty


def _reference_price(live_state: LiveStateSnapshot, symbol: str) -> float | None:
    value = dict(live_state.prices).get(symbol)
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


__all__ = [
    "DISABLED_REASON",
    "EnvelopeVerdict",
    "FrozenDailySignal",
    "HeadroomEvaluator",
    "INTRADAY_DECISIONING_SCHEMA_VERSION",
    "IntradayContractError",
    "IntradayEnvelopeLimits",
    "IntradayLeakError",
    "IntradayTickResult",
    "LiveStateSnapshot",
    "OrderIntent",
    "ReservedCashError",
    "SessionEnvelopeCounters",
    "SessionStartSnapshot",
    "SkippedIntent",
    "build_intraday_context",
    "compute_parent_intent_id",
    "default_decision_stages",
    "evaluate_entry_envelope",
    "frozen_score_diagnostic_stages",
    "intraday_decisioning_enabled",
    "run_frozen_score_diagnostic_tick",
    "run_intraday_decision_tick",
]
