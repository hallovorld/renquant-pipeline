"""StrategyConfig schema — typed dangerous top level, warn-first rollout.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.2 /
S1-PR3 ("config typos fail at load, not mid-trade"; risk control: schema
is additive first — warn-only — fail-closed after one clean week);
prototype proven against the real production config + golden:
scripts/engineering/config_schema_prototype.py (orchestrator PR #112 batch).

Strategy: don't boil the ~64-top-key ocean. Type the DANGEROUS subset
(regime thresholds, hold/position caps, watchlist) with ranges that make
classic typo classes impossible (sign flips, 10× slips, string-for-number),
and keep ``extra="allow"`` everywhere so untyped keys pass through with
telemetry. Gradual typing: each new typed field shrinks ``extra_top_keys``.

Rollout contract:
  * ``mode="warn"``  (default) — never raises; returns a ValidationReport
    and logs each violation. Wire this first.
  * ``mode="strict"`` — raises ConfigSchemaError on any violation. Flip
    after one clean week of warn-mode telemetry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("kernel.config_schema")


class ConfigSchemaError(ValueError):
    """Strict-mode validation failure. Fail-closed: do not trade on a
    config that doesn't parse."""


class RegimeSchema(BaseModel):
    """The regime-detection thresholds — the highest-blast-radius block
    (false-BEAR forensics class). Ranges encode sign + magnitude sanity."""
    model_config = ConfigDict(extra="allow")
    bear_vol_threshold: float = Field(gt=0, lt=2)
    bear_return_threshold: float = Field(gt=-1, lt=0)
    bear_vol_threshold_5d: float = Field(gt=0, lt=2)
    bear_return_threshold_5d: float = Field(gt=-1, lt=0)
    transition_uncertainty_bars: int = Field(ge=0, le=30)
    bear_short_route_require_both: bool


class StrategyConfigSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_name: str
    watchlist: list[str] = Field(min_length=1)
    benchmark: str
    wash_sale_days: int = Field(ge=0, le=61)
    min_hold_days: int = Field(ge=0, le=120)
    max_hold_days: int = Field(ge=0, le=2000)
    max_concurrent_positions: int = Field(ge=1, le=50)
    regime: RegimeSchema

    @property
    def extra_top_keys(self) -> list[str]:
        return sorted((self.model_extra or {}).keys())


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: tuple[str, ...] = ()
    extra_top_keys: tuple[str, ...] = ()
    config: StrategyConfigSchema | None = field(default=None, repr=False)


def _format_errors(exc: ValidationError) -> tuple[str, ...]:
    out = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "<root>"
        out.append(f"{loc}: {e['msg']} (got {e.get('input')!r})")
    return tuple(out)


def validate_strategy_config(
    raw: dict[str, Any], *, mode: str = "warn",
) -> ValidationReport:
    """Validate a loaded strategy config dict against the typed subset.

    warn mode: log every violation, never raise — safe to wire into the
    live load path immediately. strict mode: raise ConfigSchemaError.
    """
    if mode not in ("warn", "strict"):
        raise ValueError(f"mode must be 'warn' or 'strict', got {mode!r}")
    try:
        cfg = StrategyConfigSchema(**raw)
    except ValidationError as exc:
        errors = _format_errors(exc)
        if mode == "strict":
            raise ConfigSchemaError(
                "strategy config failed schema validation (fail-closed): "
                + "; ".join(errors)
            ) from exc
        for e in errors:
            log.warning("config schema violation (warn-only): %s", e)
        return ValidationReport(ok=False, errors=errors)
    extras = tuple(cfg.extra_top_keys)
    if extras:
        log.info("config schema: %d untyped top-level key(s) passed through "
                 "(gradual-typing telemetry): %s", len(extras), list(extras))
    return ValidationReport(ok=True, extra_top_keys=extras, config=cfg)
