"""P-STATE-FILE — live_state.{broker}.json parses (or absent)."""
from __future__ import annotations

import json

from renquant_pipeline.kernel.preflight import PreflightCheck  # noqa: PLC0415 (legacy bridge)

from ..base import PreflightTask
from ..ctx import PreflightContext


class StateFileTask(PreflightTask):
    """P-STATE-FILE — live_state.{broker}.json parses (or is absent for first run).

    Behavior parity with ``kernel.preflight._check_state_file``:
      - no broker_name → soft pass ("dry-run; skip")
      - state_paths module unavailable → soft pass ("skip")
      - file absent → soft pass ("first run?")
      - file present + unparseable → HARD FAIL
      - file present + parses → HARD PASS
    """

    check_name = "P-STATE-FILE"

    def check(self, ctx: PreflightContext) -> PreflightCheck:
        if not ctx.broker_name:
            return PreflightCheck(
                self.check_name, "soft", True, "no broker_name (dry-run); skip",
            )
        try:
            from renquant_pipeline.kernel.state_paths import resolve_live_state_read  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", True,
                f"state_paths unavailable: {exc}; skip",
            )
        p, _used_legacy = resolve_live_state_read(ctx.strategy_dir, ctx.broker_name)
        if not p.exists():
            return PreflightCheck(
                self.check_name, "soft", True,
                f"state file absent at {p.name} (first run?)",
            )
        try:
            raw = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "hard", False,
                f"state file unreadable {p.name}: {exc}",
            )
        # LiveStateV2 typed validation (eng plan §III.4 S1-PR1 wiring,
        # additive): parse through the one schema authority. Quarantined
        # unknown keys and shape errors surface as SOFT findings during
        # the warn window — the legacy hard pass/fail contract above is
        # unchanged (raw-JSON parseability is still the hard gate).
        try:
            from renquant_pipeline.kernel.live_state_v2 import LiveStateV2  # noqa: PLC0415

            state = LiveStateV2.parse(raw)
        except Exception as exc:  # noqa: BLE001
            return PreflightCheck(
                self.check_name, "soft", False,
                f"{p.name} parses as JSON but FAILS LiveStateV2 schema "
                f"(warn window — investigate before the strict flip): {exc}",
            )
        if state.extra_quarantine:
            return PreflightCheck(
                self.check_name, "soft", True,
                f"loaded {p.name}; {len(state.extra_quarantine)} unknown "
                f"top-level key(s) quarantined (schema lag or foreign "
                f"writer): {sorted(state.extra_quarantine)}",
                details={"quarantined_keys": sorted(state.extra_quarantine)},
            )
        return PreflightCheck(
            self.check_name, "hard", True,
            f"loaded {p.name} (LiveStateV2 valid, "
            f"{len(state.holdings)} holdings)",
        )
