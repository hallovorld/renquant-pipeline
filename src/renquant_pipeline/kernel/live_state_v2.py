"""LiveStateV2 — typed live-state schema with ONE parse/serialize pair.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.4 /
S1-PR1 + errata D acceptance matrix; prototype proven against the real
production state: scripts/engineering/live_state_v2_prototype.py
(orchestrator PR #112 batch).

Why: adding ONE live-state field took 9 manual touch points (measured on
``protection_breaches``, umbrella PR #294) because the state is a
schema-less dict with hand-written round-trips scattered across the
runner. This module is the single place where live-state JSON becomes a
typed object and back.

Wire-format decision (rollback-read safety, errata D): the ON-DISK shape
stays v1-flat (``entry_dates`` / ``sell_streaks`` / ... per-ticker dicts)
plus a ``schema_version: 2`` stamp. Old runner code reading a file written
by this module sees exactly the keys it always read; new code gets the
typed model. Per-ticker fields live in ONE mapping (``_HOLDING_V1_KEYS``),
so adding a holding field = one schema line + one mapping entry, then the
errata-D test matrix must pass (tests/test_live_state_v2.py).

Unknown-field policy: top-level keys this schema does not know are
QUARANTINED (kept, surfaced in ``extra_quarantine``, re-emitted on
serialize) — never silently dropped, so a newer writer's fields survive an
older reader's rewrite, and never silently accepted into the typed model.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

log = logging.getLogger("kernel.live_state_v2")

SCHEMA_VERSION = 2


class EntrySignalV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank_score: float | None = None
    panel_score: float | None = None
    kelly_target_pct: float | None = None
    regime: str | None = None      # entry-regime max_hold anchor (incident #5)


class HoldingV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_date: str
    sell_streak: int = 0
    protection_breaches: int = 0   # the PR-#294 field — the 1-line example
    position_hwm: float | None = None
    entry_signal: EntrySignalV2 | None = None


# Holding field → v1 per-ticker collection. Adding a HoldingV2 field
# requires exactly one entry here; parse/serialize derive everything else.
_HOLDING_V1_KEYS: dict[str, str] = {
    "entry_date": "entry_dates",
    "sell_streak": "sell_streaks",
    "protection_breaches": "protection_breaches",
    "position_hwm": "position_hwm",
    "entry_signal": "entry_signals",
}

_HOLDING_DEFAULTS = {
    name: field.get_default() for name, field in HoldingV2.model_fields.items()
}

# Known v1 top-level keys; everything else is quarantined on parse.
_TOP_LEVEL_V1 = {
    "schema_version", "regime", "regime_confidence", "high_water_mark",
    "last_sell_dates", "last_stop_exit_dates", "skip_buys",
    "monitor_state", "regime_state", "stop_orders",
    *_HOLDING_V1_KEYS.values(),
}


class MonitorStateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    no_trade_streak: int = 0
    no_candidate_streak: int = 0
    last_activity_date: str | None = None
    first_trade_date: str | None = None
    no_trade_streak_source: str | None = None
    last_fill_date: str | None = None
    last_check_date: str | None = None


class RegimeStateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regime: str = "UNKNOWN"
    confidence: float = 0.0
    in_transition: bool = False
    countdown: int = 0
    cusum_pos: float = 0.0
    cusum_neg: float = 0.0
    cooldown_start: str | None = None


class LiveStateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    regime: str = "UNKNOWN"
    regime_confidence: float = 0.0
    high_water_mark: float | None = None
    holdings: dict[str, HoldingV2] = {}
    last_sell_dates: dict[str, str] = {}
    last_stop_exit_dates: dict[str, str] = {}
    skip_buys: bool = False
    monitor_state: MonitorStateV2 | None = None
    regime_state: RegimeStateV2 | None = None
    # Z9 broker-side stop bookkeeping; shape owned by the umbrella runner —
    # typed passthrough until the Z9 record gets its own model.
    stop_orders: dict[str, Any] = {}
    extra_quarantine: dict[str, Any] = {}

    # ── parse: the ONE v1-flat → typed migration ────────────────────────

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "LiveStateV2":
        """v1-flat dict (with or without the v2 stamp) → typed state.

        Missing keys take schema defaults (old files auto-migrate);
        unknown keys are quarantined loudly, never dropped.
        """
        holdings: dict[str, HoldingV2] = {}
        for ticker, entry_date in (raw.get("entry_dates") or {}).items():
            kw: dict[str, Any] = {"entry_date": str(entry_date)}
            for field, v1_key in _HOLDING_V1_KEYS.items():
                if field == "entry_date":
                    continue
                val = (raw.get(v1_key) or {}).get(ticker)
                if field == "entry_signal":
                    kw[field] = EntrySignalV2(**val) if val else None
                elif val is None:
                    kw[field] = _HOLDING_DEFAULTS[field]
                else:
                    kw[field] = val
            holdings[str(ticker)] = HoldingV2(**kw)

        quarantine = {k: v for k, v in raw.items() if k not in _TOP_LEVEL_V1}
        if quarantine:
            log.warning("live_state: %d unknown top-level key(s) quarantined "
                        "(preserved, not parsed): %s",
                        len(quarantine), sorted(quarantine))

        monitor = raw.get("monitor_state")
        regime_state = raw.get("regime_state")
        return cls(
            regime=str(raw.get("regime") or "UNKNOWN"),
            regime_confidence=float(raw.get("regime_confidence") or 0.0),
            high_water_mark=raw.get("high_water_mark"),
            holdings=holdings,
            last_sell_dates=dict(raw.get("last_sell_dates") or {}),
            last_stop_exit_dates=dict(raw.get("last_stop_exit_dates") or {}),
            skip_buys=bool(raw.get("skip_buys", False)),
            monitor_state=MonitorStateV2(**monitor) if monitor else None,
            regime_state=RegimeStateV2(**regime_state) if regime_state else None,
            stop_orders=dict(raw.get("stop_orders") or {}),
            extra_quarantine=quarantine,
        )

    # ── serialize: the ONE typed → v1-flat wire shape ───────────────────

    def to_wire(self) -> dict[str, Any]:
        """v1-compatible flat dict + schema_version stamp.

        Old readers see exactly the v1 keys; quarantined foreign keys are
        re-emitted so a rewrite never loses another writer's fields.
        """
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "high_water_mark": self.high_water_mark,
            "last_sell_dates": dict(self.last_sell_dates),
            "last_stop_exit_dates": dict(self.last_stop_exit_dates),
            "skip_buys": self.skip_buys,
            "stop_orders": dict(self.stop_orders),
        }
        if self.monitor_state is not None:
            out["monitor_state"] = self.monitor_state.model_dump()
        if self.regime_state is not None:
            out["regime_state"] = self.regime_state.model_dump()
        for field, v1_key in _HOLDING_V1_KEYS.items():
            col: dict[str, Any] = {}
            for ticker, h in self.holdings.items():
                val = getattr(h, field)
                if field == "entry_signal":
                    if val is not None:
                        col[ticker] = val.model_dump()
                elif val is not None:
                    col[ticker] = val
            # Defaults-only collections still emit (v1 readers iterate them).
            out[v1_key] = col
        for k, v in self.extra_quarantine.items():
            out.setdefault(k, v)
        return out

    def canonical_json(self) -> str:
        return json.dumps(self.to_wire(), sort_keys=True, indent=2, default=str)


def read_live_state(path: Path) -> LiveStateV2:
    """Parse a live_state JSON file through the one authority."""
    return LiveStateV2.parse(json.loads(Path(path).read_text(encoding="utf-8")))


def write_live_state_atomic(path: Path, state: LiveStateV2) -> None:
    """Atomic tmp+rename write (errata D): a crash mid-write can never
    leave a truncated/partial live_state file — readers see the old
    complete state or the new complete state, nothing in between."""
    path = Path(path)
    payload = state.canonical_json()
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
