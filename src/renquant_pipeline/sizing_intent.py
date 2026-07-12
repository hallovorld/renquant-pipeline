"""Versioned pre-quantization sizing-intent contract.

The contract is deliberately owned by ``renquant-pipeline``: it describes the
pipeline decision before an execution adapter turns it into a broker order. It
is the evidence surface for the 104 paired one-share-floor shadow and for the
future 105 measurement path. It does not size an order, submit an order, or
enable any strategy configuration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Mapping


SIZING_INTENT_SCHEMA_VERSION = "sizing-intent-v1"
SIZING_INTENT_KIND = "sizing-intent"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SizingIntentContractError(ValueError):
    """A pre-quantization sizing record is incomplete or inconsistent."""


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise SizingIntentContractError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise SizingIntentContractError(f"{name} is required")
    return normalized


def _sha256(name: str, value: str) -> str:
    normalized = _required_text(name, value)
    if not _SHA256_RE.fullmatch(normalized):
        raise SizingIntentContractError(f"{name} must be a lowercase sha256")
    return normalized


def _finite_nonnegative(name: str, value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SizingIntentContractError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise SizingIntentContractError(f"{name} must be finite and >= 0")
    return result


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SizingIntentContractError(f"{name} must be a nonnegative integer")
    return value


def _admission_gate_outcomes(value: Mapping[str, Any]) -> dict[str, bool]:
    if not isinstance(value, Mapping) or not value:
        raise SizingIntentContractError("admission_gate_outcomes must be a non-empty mapping")
    outcomes: dict[str, bool] = {}
    for gate, passed in value.items():
        normalized_gate = _required_text("admission gate name", gate)
        if not isinstance(passed, bool):
            raise SizingIntentContractError(
                "admission_gate_outcomes values must be bool"
            )
        outcomes[normalized_gate] = passed
    return dict(sorted(outcomes.items()))


@dataclass(frozen=True)
class SizingIntentRecord:
    """One post-admission, pre-execution sizing decision.

    ``normal_buy_reservation_notional`` is the cash retained for ordinary
    buys before a deferred floor rescue is considered. A paired-shadow runner
    supplies the run/session and manifest identities; this module validates
    the pipeline-owned decision arithmetic without reconstructing it.
    """

    run_id: str
    session_id: str
    arm_id: str
    input_manifest_sha256: str
    config_sha256: str
    source: str
    ticker: str
    candidate_id: str
    candidate_rank: int
    admission_passed: bool
    admission_gate_outcomes: Mapping[str, bool]
    target_notional: float
    unrounded_quantity: float
    planned_quantity: float
    reference_price: float
    reference_price_as_of: str
    per_name_cap_notional: float
    cash_reserve_notional: float
    available_cash_before: float
    normal_buy_reservation_notional: float
    cumulative_exposure_before_notional: float
    cumulative_exposure_after_notional: float
    ordinary_buy_displacement_count: int
    outcome: str
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required_text("run_id", self.run_id))
        object.__setattr__(self, "session_id", _required_text("session_id", self.session_id))
        object.__setattr__(self, "arm_id", _required_text("arm_id", self.arm_id))
        object.__setattr__(
            self,
            "input_manifest_sha256",
            _sha256("input_manifest_sha256", self.input_manifest_sha256),
        )
        object.__setattr__(self, "config_sha256", _sha256("config_sha256", self.config_sha256))
        object.__setattr__(self, "source", _required_text("source", self.source))
        ticker = _required_text("ticker", self.ticker).upper()
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "candidate_id", _required_text("candidate_id", self.candidate_id))
        if (
            isinstance(self.candidate_rank, bool)
            or not isinstance(self.candidate_rank, int)
            or self.candidate_rank < 0
        ):
            raise SizingIntentContractError("candidate_rank must be a nonnegative integer")
        if not isinstance(self.admission_passed, bool):
            raise SizingIntentContractError("admission_passed must be bool")
        gate_outcomes = _admission_gate_outcomes(self.admission_gate_outcomes)
        object.__setattr__(self, "admission_gate_outcomes", gate_outcomes)
        if self.admission_passed != all(gate_outcomes.values()):
            raise SizingIntentContractError(
                "admission_passed must equal the conjunction of admission_gate_outcomes"
            )
        for name in (
            "target_notional",
            "unrounded_quantity",
            "planned_quantity",
            "per_name_cap_notional",
            "cash_reserve_notional",
            "available_cash_before",
            "normal_buy_reservation_notional",
            "cumulative_exposure_before_notional",
            "cumulative_exposure_after_notional",
        ):
            object.__setattr__(self, name, _finite_nonnegative(name, getattr(self, name)))
        object.__setattr__(
            self,
            "ordinary_buy_displacement_count",
            _nonnegative_int(
                "ordinary_buy_displacement_count", self.ordinary_buy_displacement_count
            ),
        )
        price = _finite_nonnegative("reference_price", self.reference_price)
        if price <= 0:
            raise SizingIntentContractError("reference_price must be > 0")
        object.__setattr__(self, "reference_price", price)
        object.__setattr__(
            self,
            "reference_price_as_of",
            _required_text("reference_price_as_of", self.reference_price_as_of),
        )
        object.__setattr__(self, "outcome", _required_text("outcome", self.outcome))
        if self.reason is not None:
            object.__setattr__(self, "reason", _required_text("reason", self.reason))

        expected_target = self.unrounded_quantity * self.reference_price
        if not math.isclose(
            self.target_notional,
            expected_target,
            rel_tol=1e-9,
            abs_tol=1e-8,
        ):
            raise SizingIntentContractError(
                "target_notional must equal unrounded_quantity * reference_price"
            )

        planned_notional = self.planned_quantity * self.reference_price
        if planned_notional > self.per_name_cap_notional + 1e-8:
            raise SizingIntentContractError("planned notional exceeds per_name_cap_notional")
        spendable = (
            self.available_cash_before
            - self.cash_reserve_notional
            - self.normal_buy_reservation_notional
        )
        if planned_notional > spendable + 1e-8:
            raise SizingIntentContractError(
                "planned notional exceeds cash remaining after reserve and ordinary-buy reservation"
            )
        expected_exposure_after = self.cumulative_exposure_before_notional + planned_notional
        if not math.isclose(
            self.cumulative_exposure_after_notional,
            expected_exposure_after,
            rel_tol=1e-9,
            abs_tol=1e-8,
        ):
            raise SizingIntentContractError(
                "cumulative_exposure_after_notional must equal cumulative exposure before plus planned notional"
            )
        if self.outcome == "emitted" and self.planned_quantity <= 0:
            raise SizingIntentContractError("emitted record requires planned_quantity > 0")
        if self.outcome != "emitted" and self.planned_quantity != 0:
            raise SizingIntentContractError("non-emitted record requires planned_quantity == 0")
        if self.outcome != "emitted" and self.reason is None:
            raise SizingIntentContractError("non-emitted record requires a reason")

    def to_dict(self) -> dict[str, Any]:
        """Return the schema-stamped JSON-safe representation."""
        record = asdict(self)
        record["admission_gate_outcomes"] = dict(self.admission_gate_outcomes)
        return {
            "schema_version": SIZING_INTENT_SCHEMA_VERSION,
            "kind": SIZING_INTENT_KIND,
            **record,
        }


def parse_sizing_intent_record(payload: Mapping[str, Any]) -> SizingIntentRecord:
    """Validate and deserialize one externally supplied sizing-intent record."""
    if not isinstance(payload, Mapping):
        raise SizingIntentContractError("sizing intent payload must be a mapping")
    if payload.get("schema_version") != SIZING_INTENT_SCHEMA_VERSION:
        raise SizingIntentContractError("unsupported sizing intent schema_version")
    if payload.get("kind") != SIZING_INTENT_KIND:
        raise SizingIntentContractError("unsupported sizing intent kind")
    expected_fields = {"schema_version", "kind", *SizingIntentRecord.__dataclass_fields__}
    unknown_fields = sorted(set(payload) - expected_fields)
    if unknown_fields:
        raise SizingIntentContractError(
            f"sizing intent payload carries unknown fields: {unknown_fields}"
        )
    fields = {
        name: payload.get(name)
        for name in SizingIntentRecord.__dataclass_fields__
    }
    return SizingIntentRecord(**fields)


__all__ = [
    "SIZING_INTENT_KIND",
    "SIZING_INTENT_SCHEMA_VERSION",
    "SizingIntentContractError",
    "SizingIntentRecord",
    "parse_sizing_intent_record",
]
