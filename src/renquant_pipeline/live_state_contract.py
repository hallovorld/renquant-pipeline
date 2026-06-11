"""Native live-state read contract.

This module is intentionally read-only. It gives orchestrator/native live
tooling one stable shape for broker-isolated live_state files and the
append-only live_state_snapshots table without importing umbrella runner code.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from .kernel.persistence import load_latest_live_state
from .state_paths import resolve_live_state_read


@dataclass(frozen=True)
class LiveStateContract:
    schema_version: int
    source: str
    state: dict[str, Any]
    account_snapshot: dict[str, Any]
    path: str | None = None
    used_legacy: bool = False
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source": self.source,
            "state": dict(self.state),
            "account_snapshot": dict(self.account_snapshot),
            "used_legacy": self.used_legacy,
            "warnings": list(self.warnings),
        }
        if self.path is not None:
            payload["path"] = self.path
        return payload


def _state_from_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"live_state file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"live_state file must contain a JSON object: {path}")
    return dict(payload)


def _normalize_position(raw_ticker: str, raw_position: Any) -> dict[str, Any] | None:
    if not isinstance(raw_position, dict):
        raise ValueError(f"position row must be an object: {raw_ticker}")
    row = dict(raw_position)
    ticker = str(row.get("ticker") or row.get("symbol") or raw_ticker)
    quantity = row.get("quantity", row.get("qty", row.get("shares")))
    if quantity is not None:
        try:
            if float(quantity) == 0.0:
                return None
        except (TypeError, ValueError):
            pass
        row["quantity"] = quantity
        row.pop("qty", None)
        row.pop("shares", None)
    row["ticker"] = ticker
    return row


def _positions_from_mapping(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for raw_ticker, raw_position in value.items():
        row = _normalize_position(str(raw_ticker), raw_position)
        if row:
            positions[row["ticker"]] = row
    return positions


def _positions_from_sequence(value: list[Any]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for idx, raw_position in enumerate(value):
        if not isinstance(raw_position, dict):
            raise ValueError(f"position row must be an object: positions[{idx}]")
        ticker = raw_position.get("ticker") or raw_position.get("symbol")
        if not ticker:
            raise ValueError(f"position row missing ticker/symbol: positions[{idx}]")
        row = _normalize_position(str(ticker), raw_position)
        if row:
            positions[row["ticker"]] = row
    return positions


def account_snapshot_from_live_state(state: dict[str, Any]) -> dict[str, Any]:
    """Extract account_snapshot only from explicit account/position fields.

    ``position_hwm`` and stop/high-water metadata are deliberately ignored: they
    can identify tickers that have been tracked, but they are not broker
    quantities and must not be promoted into live holdings.
    """
    explicit = state.get("account_snapshot")
    if explicit is not None:
        if not isinstance(explicit, dict):
            raise ValueError("live_state.account_snapshot must be an object")
        return dict(explicit)

    raw_positions = state.get("positions")
    if raw_positions is None:
        raw_positions = state.get("holdings")
    if raw_positions is None:
        return {}

    if isinstance(raw_positions, dict):
        positions = _positions_from_mapping(raw_positions)
    elif isinstance(raw_positions, list):
        positions = _positions_from_sequence(raw_positions)
    else:
        raise ValueError("live_state positions/holdings must be an object or list")

    snapshot: dict[str, Any] = {"positions": positions}
    for field_name in ("cash", "portfolio_value", "equity", "buying_power"):
        if field_name in state:
            snapshot[field_name] = state[field_name]
    return snapshot


def _load_db_state(
    runs_db: str | Path | sqlite3.Connection | None,
    *,
    strategy: str,
    max_age_days: int | None,
) -> dict[str, Any] | None:
    if runs_db is None:
        return None
    if isinstance(runs_db, sqlite3.Connection):
        return load_latest_live_state(
            runs_db,
            strategy=strategy,
            max_age_days=max_age_days,
        )

    path = Path(runs_db)
    if not path.exists():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            return load_latest_live_state(
                conn,
                strategy=strategy,
                max_age_days=max_age_days,
            )
    except sqlite3.Error:
        return None


def load_live_state_contract(
    strategy_dir: str | Path,
    broker_name: str | None,
    *,
    runs_db: str | Path | sqlite3.Connection | None = None,
    strategy: str = "renquant_104",
    max_age_days: int | None = None,
) -> LiveStateContract:
    """Load broker live_state with DB fallback and normalized account fields."""
    state_path, used_legacy = resolve_live_state_read(strategy_dir, broker_name)
    warnings: list[str] = []
    if state_path.exists():
        try:
            state = _state_from_json(state_path)
            return LiveStateContract(
                schema_version=1,
                source="live_state_file",
                path=str(state_path),
                used_legacy=used_legacy,
                state=state,
                account_snapshot=account_snapshot_from_live_state(state),
            )
        except ValueError as exc:
            warnings.append(str(exc))
            db_state = _load_db_state(
                runs_db,
                strategy=strategy,
                max_age_days=max_age_days,
            )
            if db_state is None:
                raise
            return LiveStateContract(
                schema_version=1,
                source="live_state_snapshots_db",
                path=str(state_path),
                used_legacy=used_legacy,
                state=dict(db_state),
                account_snapshot=account_snapshot_from_live_state(db_state),
                warnings=tuple(warnings),
            )

    db_state = _load_db_state(
        runs_db,
        strategy=strategy,
        max_age_days=max_age_days,
    )
    if db_state is not None:
        return LiveStateContract(
            schema_version=1,
            source="live_state_snapshots_db",
            path=str(state_path),
            used_legacy=used_legacy,
            state=dict(db_state),
            account_snapshot=account_snapshot_from_live_state(db_state),
        )

    return LiveStateContract(
        schema_version=1,
        source="empty",
        path=str(state_path),
        used_legacy=used_legacy,
        state={},
        account_snapshot={},
    )


__all__ = [
    "LiveStateContract",
    "account_snapshot_from_live_state",
    "load_live_state_contract",
]
