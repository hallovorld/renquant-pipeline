"""Tests for the D6 protocol replay conventions (opt-in harness extensions).

Covers the seven D6 requirements the #445 gap list identified against the
pre-D6 harness:

1. Default mode UNCHANGED — evidence JSON byte-identical to the artifact
   pinned from the pre-change code (``tests/fixtures/``).
2. Stateful accounting correctness — exact cash conservation:
   ``cash + positions value == PV`` with costs/taxes flowing through cash.
3. Tax lot short/long boundary per the D6 §1.1 frozen convention
   (short 50% / long 32%, lot holding period decides; rotation
   ``tax_drag()`` convention — losses give zero drag).
4. Integer executed-state invariant — floor-only rounding, executed
   weights never above cap post-round, post-round state carries.
5. Sector/name cap projection is DOWN-ONLY with per-session breach
   counters (no silent allowance).
6. Evidence JSON extended additively (new keys only when engaged).
7. CLI flag wiring + validation.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from renquant_pipeline.kernel.portfolio_qp.allocator_replay import (  # noqa: E402
    AllocatorReplayBar,
    PortfolioState,
    ReplayConventions,
    apply_d6_cap_projection,
    replay_all,
    replay_one_allocator,
    sector_map_coverage_gap,
)
from renquant_pipeline.kernel.portfolio_qp.baseline_allocators import (  # noqa: E402
    AllocatorResult,
    equal_weight_top_k,
)
from renquant_pipeline.kernel.portfolio_qp.constraint_snapshot import (  # noqa: E402
    ConstraintSnapshot,
)
from renquant_pipeline.kernel.portfolio_qp.run_ab_replay import (  # noqa: E402
    main,
    run_replay,
)

from fixtures.d6_default_bars import (  # noqa: E402
    FIXTURE_ALLOCATORS,
    FIXTURE_INCUMBENT,
    FIXTURE_PBO_N_SLICES,
    build_default_fixture_bars,
)

FIXTURE_EVIDENCE = Path(__file__).parent / "fixtures" / "ab_replay_default_evidence.json"
# Platform the pinned artifact was generated on (see d6_default_bars.py).
# Byte-identity only holds where numpy/BLAS reduction order matches the
# minting build; other platforms run the ULP-tolerant deep comparison.
FIXTURE_PIN_PLATFORM = "darwin"


def _assert_json_equal(a, b, path: str = "$") -> None:
    """Deep-compare two parsed JSON trees.

    EXACT on structure (key sets, list lengths), strings, booleans,
    None, and integers; floats must agree within rel 1e-9 / abs 1e-12
    — orders of magnitude tighter than any behavioral change, loose
    enough to absorb cross-platform numpy/BLAS reduction-order ULP
    noise (CI run 29071927009: last-digit repr differences ubuntu vs
    darwin). This is the platform-independent form of the default-mode
    pin; the byte-identity test below enforces the strict form on the
    platform that minted the fixture.
    """
    if isinstance(a, dict) or isinstance(b, dict):
        assert isinstance(a, dict) and isinstance(b, dict), path
        assert sorted(a.keys()) == sorted(b.keys()), (
            f"{path}: key sets differ: {sorted(a)} vs {sorted(b)}"
        )
        for k in a:
            _assert_json_equal(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) or isinstance(b, list):
        assert isinstance(a, list) and isinstance(b, list), path
        assert len(a) == len(b), f"{path}: lengths {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            _assert_json_equal(x, y, f"{path}[{i}]")
    elif isinstance(a, bool) or isinstance(b, bool) or a is None or b is None \
            or isinstance(a, str) or isinstance(b, str):
        assert a == b, f"{path}: {a!r} != {b!r}"
    elif isinstance(a, int) and isinstance(b, int):
        assert a == b, f"{path}: {a!r} != {b!r}"
    else:
        assert a == pytest.approx(b, rel=1e-9, abs=1e-12), (
            f"{path}: {a!r} != {b!r}"
        )


# ── helpers ─────────────────────────────────────────────────────────


def _snap(n: int, tickers=None) -> ConstraintSnapshot:
    tickers = tickers or tuple(f"T{i}" for i in range(n))
    return ConstraintSnapshot(
        n=n,
        tickers=tuple(tickers),
        w_current=np.zeros(n),
        w_upper_hard=np.full(n, 1.0),
        w_upper=np.full(n, 1.0),
        w_lower=0.0,
        dw_max=np.full(n, 2.0),
        cash_reserve=0.0,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=1.0,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
    )


def _snap_l3(
    n: int,
    tickers,
    *,
    cap: float = 1.0,
    cash_reserve: float = 0.0,
    sector_indicator=None,
    sector_cap_vec=None,
    corr_group_pairs=(),
) -> ConstraintSnapshot:
    """Snapshot builder with the knobs the RFC §2.3 L3 tests exercise."""
    return ConstraintSnapshot(
        n=n,
        tickers=tuple(tickers),
        w_current=np.zeros(n),
        w_upper_hard=np.full(n, cap),
        w_upper=np.full(n, cap),
        w_lower=0.0,
        dw_max=np.full(n, 2.0),
        cash_reserve=cash_reserve,
        turnover_max=None,
        drawdown=0.0,
        drawdown_limit=1.0,
        gross_max=None,
        wash_sale_mask=np.zeros(n, dtype=bool),
        sector_indicator=(
            np.asarray(sector_indicator, dtype=float)
            if sector_indicator is not None else None
        ),
        sector_cap_vec=(
            np.asarray(sector_cap_vec, dtype=float)
            if sector_cap_vec is not None else None
        ),
        sector_names=(
            tuple(f"S{i}" for i in range(len(sector_cap_vec)))
            if sector_cap_vec is not None else None
        ),
        corr_group_pairs=tuple(corr_group_pairs),
    )


def _bar(
    date: str,
    *,
    tickers,
    mu,
    fwd_return,
    cost_bps: float = 0.0,
    prices=None,
    regime=None,
    cap: float = 1.0,
    cash_reserve: float = 0.0,
    sector_indicator=None,
    sector_cap_vec=None,
    corr_group_pairs=(),
    sigma=None,
) -> AllocatorReplayBar:
    n = len(tickers)
    return AllocatorReplayBar(
        bar_date=date,
        snap=_snap_l3(
            n, tuple(tickers), cap=cap, cash_reserve=cash_reserve,
            sector_indicator=sector_indicator, sector_cap_vec=sector_cap_vec,
            corr_group_pairs=corr_group_pairs,
        ),
        mu=np.asarray(mu, dtype=float),
        sigma=(
            np.asarray(sigma, dtype=float) if sigma is not None
            else np.full(n, 0.10)
        ),
        fwd_return=np.asarray(fwd_return, dtype=float),
        regime=regime,
        cost_per_trade_bps=cost_bps,
        prices=np.asarray(prices, dtype=float) if prices is not None else None,
    )


def _fixed_target_allocator(targets_by_date: dict[str, dict[str, float]]):
    """Stub allocator returning a fixed per-date target weight map."""

    def alloc(snap, *, mu, sigma=None):  # noqa: ARG001
        per_date = targets_by_date.get("*") or {}
        target = np.zeros(snap.n)
        for i, tk in enumerate(snap.tickers):
            target[i] = float(per_date.get(tk, 0.0))
        return AllocatorResult(
            delta_w=target - snap.w_current,
            target_w=target,
            status="optimal",
            selected_indices=tuple(np.where(target > 0)[0]),
        )

    return alloc


def _scripted_allocator(script: list[dict[str, float]]):
    """Stub allocator that plays back a per-call sequence of targets."""
    calls = {"i": 0}

    def alloc(snap, *, mu, sigma=None):  # noqa: ARG001
        step = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        target = np.zeros(snap.n)
        for i, tk in enumerate(snap.tickers):
            target[i] = float(step.get(tk, 0.0))
        return AllocatorResult(
            delta_w=target - snap.w_current,
            target_w=target,
            status="optimal",
            selected_indices=tuple(np.where(target > 0)[0]),
        )

    return alloc


# ── 1. Default mode unchanged (byte-identity pin) ───────────────────


class TestDefaultModeUnchanged:
    def test_default_evidence_matches_pre_change_pin(self):
        """The pinned artifact was generated on the PRE-D6 code
        (origin/main @ f6e818c). The opt-in conventions must not change
        default-mode evidence: schema (key trees), strings, booleans and
        integers EXACTLY; floats within reduction-order ULP noise
        (platform-independent form of the pin — runs everywhere,
        including CI's ubuntu/pip numpy)."""
        payload = run_replay(
            build_default_fixture_bars(),
            list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
        )
        rendered = json.loads(json.dumps(payload, indent=2, sort_keys=True))
        pinned = json.loads(FIXTURE_EVIDENCE.read_text())
        _assert_json_equal(rendered, pinned)

    @pytest.mark.skipif(
        sys.platform != FIXTURE_PIN_PLATFORM,
        reason=(
            "byte-identity only holds on the platform whose numpy/BLAS "
            "build minted the fixture; other platforms differ in float "
            "reduction order at the last ULP and are covered by "
            "test_default_evidence_matches_pre_change_pin"
        ),
    )
    def test_default_evidence_byte_identical_on_pin_platform(self):
        """Strict form of the pin: on the minting platform the opt-in
        conventions must not move a single byte of default evidence."""
        payload = run_replay(
            build_default_fixture_bars(),
            list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
        )
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        assert rendered == FIXTURE_EVIDENCE.read_text(), (
            "default-mode evidence JSON changed vs the pre-D6 pin — the "
            "conventions are supposed to be strictly opt-in"
        )

    def test_default_evidence_has_no_convention_keys(self):
        payload = run_replay(
            build_default_fixture_bars(),
            list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
        )
        assert "replay_conventions" not in payload
        for name, block in payload["per_allocator"].items():
            for key in (
                "deployed_fraction", "mean_deployed_fraction", "tax_paid",
                "total_tax_paid", "cost_paid", "total_cost_paid",
                "E_executed", "integer_residual", "name_cap_breaches",
                "sector_cap_breaches", "off_universe_liquidations",
            ):
                assert key not in block, (name, key)

    def test_all_defaults_conventions_object_is_inert(self):
        """A ReplayConventions() with nothing enabled routes through the
        original stateless path and produces identical results."""
        bars = build_default_fixture_bars()
        base = replay_one_allocator("eq", equal_weight_top_k, bars)
        inert = replay_one_allocator(
            "eq", equal_weight_top_k, bars, ReplayConventions(),
        )
        assert base.daily_returns_net == inert.daily_returns_net
        assert base.turnover == inert.turnover
        assert base.to_dict() == inert.to_dict()


# ── validation ──────────────────────────────────────────────────────


class TestConventionsValidation:
    def test_tax_requires_stateful(self):
        with pytest.raises(ValueError, match="tax=True requires stateful"):
            ReplayConventions(tax=True)

    def test_integer_shares_requires_stateful(self):
        with pytest.raises(ValueError, match="integer_shares=True requires"):
            ReplayConventions(integer_shares=True)

    def test_initial_capital_positive(self):
        with pytest.raises(ValueError, match="initial_capital"):
            ReplayConventions(stateful=True, initial_capital=0.0)


# ── 2. Stateful accounting ──────────────────────────────────────────


class TestStatefulAccounting:
    def test_cash_conservation_exact_with_cost_and_tax(self):
        """Hand-computed 3-session chain: cash + positions == PV at every
        step, with 5 bp/side cost and D6 tax flowing through cash."""
        tickers = ("AAA", "BBB")
        alloc = _scripted_allocator([
            {"AAA": 0.60},              # s0: buy 60%
            {"AAA": 0.60},              # s1: hold (rebalance drift only)
            {},                         # s2: liquidate
        ])
        bars = [
            _bar("2026-01-02", tickers=tickers, mu=[0.05, 0.01],
                 fwd_return=[0.10, 0.0], cost_bps=5.0),
            _bar("2026-01-03", tickers=tickers, mu=[0.05, 0.01],
                 fwd_return=[0.0, 0.0], cost_bps=5.0),
            _bar("2026-01-04", tickers=tickers, mu=[-0.05, -0.01],
                 fwd_return=[0.0, 0.0], cost_bps=5.0),
        ]
        conv = ReplayConventions(stateful=True, tax=True, initial_capital=10_000.0)
        res = replay_one_allocator("s", alloc, bars, conv)

        bps = 5.0 * 1e-4
        # s0: buy 6000; cost 6000×5bp=3; position → ×1.10 = 6600
        cash0 = 10_000.0 - 6_000.0 - 6_000.0 * bps
        pos0 = 6_600.0
        assert res.cash_series[0] == pytest.approx(cash0, abs=1e-9)
        assert res.positions_value_series[0] == pytest.approx(pos0, abs=1e-9)

        # s1: pv = cash0 + 6600; target 0.6·pv < 6600 → partial SELL.
        pv1 = cash0 + pos0
        target1 = 0.60 * pv1
        sell1 = pos0 - target1
        # realized gain on the sold slice: basis fraction = 6000/6600
        gain1 = sell1 * (1.0 - 6_000.0 / 6_600.0)
        tax1 = gain1 * 0.50            # 1 day held → short rate
        cost1 = sell1 * bps
        cash1 = cash0 + sell1 - tax1 - cost1
        assert res.tax_paid[1] == pytest.approx(tax1, abs=1e-9)
        assert res.cost_paid[1] == pytest.approx(cost1, abs=1e-9)
        assert res.cash_series[1] == pytest.approx(cash1, abs=1e-9)
        assert res.positions_value_series[1] == pytest.approx(target1, abs=1e-9)

        # s2: liquidate. remaining basis × remaining value.
        pos2 = target1
        basis2 = 6_000.0 * (target1 / 6_600.0)
        gain2 = pos2 - basis2
        tax2 = gain2 * 0.50
        cost2 = pos2 * bps
        cash2 = cash1 + pos2 - tax2 - cost2
        assert res.cash_series[2] == pytest.approx(cash2, abs=1e-9)
        assert res.positions_value_series[2] == pytest.approx(0.0, abs=1e-9)

        # Conservation identity: PV_close == initial × Π(1+daily), exact.
        pv_close = res.final_state.portfolio_value
        compounded = 10_000.0 * float(
            np.prod(1.0 + np.asarray(res.daily_returns_net))
        )
        assert pv_close == pytest.approx(compounded, abs=1e-6)
        assert pv_close == pytest.approx(cash2, abs=1e-9)

    def test_deployed_fraction_distinct_from_turnover(self):
        """#445 gap 3: statelessly, deployed fraction ≡ turnover. In
        stateful mode a held book has zero turnover but non-zero
        deployment."""
        tickers = ("AAA",)
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50}})
        bars = [
            _bar(f"2026-01-{d:02d}", tickers=tickers, mu=[0.05],
                 fwd_return=[0.0])
            for d in range(2, 7)
        ]
        conv = ReplayConventions(stateful=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.turnover[0] == pytest.approx(0.50, abs=1e-12)
        # Sessions 2..5: target == carried weights → NO trades…
        for t in res.turnover[1:]:
            assert t == pytest.approx(0.0, abs=1e-12)
        # …but the book stays deployed at 50%.
        for d in res.deployed_fraction:
            assert d == pytest.approx(0.50, abs=1e-12)

    def test_allocator_receives_carried_weights(self):
        """Hysteresis is evaluable: the session snapshot's w_current is
        the carried state, not zeros."""
        seen: list[np.ndarray] = []

        def spy(snap, *, mu, sigma=None):  # noqa: ARG001
            seen.append(np.asarray(snap.w_current, dtype=float).copy())
            target = np.full(snap.n, 0.30)
            return AllocatorResult(
                delta_w=target - snap.w_current,
                target_w=target,
                status="optimal",
                selected_indices=(0,),
            )

        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[0.0]),
            _bar("2026-01-03", tickers=("AAA",), mu=[0.05], fwd_return=[0.0]),
        ]
        replay_one_allocator("spy", spy, bars, ReplayConventions(stateful=True))
        assert seen[0][0] == pytest.approx(0.0)
        assert seen[1][0] == pytest.approx(0.30, abs=1e-12)

    def test_stateless_default_gives_fresh_book_every_bar(self):
        """Contrast pin for the gap: without --stateful the allocator
        always sees w_current == 0 (deployed fraction ≡ turnover)."""
        seen: list[float] = []

        def spy(snap, *, mu, sigma=None):  # noqa: ARG001
            seen.append(float(np.sum(snap.w_current)))
            target = np.full(snap.n, 0.30)
            return AllocatorResult(
                delta_w=target - snap.w_current,
                target_w=target,
                status="optimal",
                selected_indices=(0,),
            )

        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[0.0]),
            _bar("2026-01-03", tickers=("AAA",), mu=[0.05], fwd_return=[0.0]),
        ]
        replay_one_allocator("spy", spy, bars)
        assert seen == [0.0, 0.0]

    def test_off_universe_position_is_liquidated_and_counted(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50, "BBB": 0.50}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.05, 0.05],
                 fwd_return=[0.0, 0.0]),
            # AAA disappears from the session universe.
            _bar("2026-01-03", tickers=("BBB",), mu=[0.05],
                 fwd_return=[0.0]),
        ]
        conv = ReplayConventions(stateful=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.off_universe_liquidations == 1
        st = res.final_state
        assert "AAA" not in st.lots
        # Zero-return, zero-cost exit → cash conserved exactly.
        assert st.portfolio_value == pytest.approx(10_000.0, abs=1e-9)


# ── 3. Tax lot convention ───────────────────────────────────────────


def _tax_boundary_run(exit_date: str) -> float:
    """Buy 50% on 2025-01-02 with a +20% mark, sell all on exit_date;
    return the tax charged on the exit session."""
    alloc = _scripted_allocator([{"AAA": 0.50}, {}])
    bars = [
        _bar("2025-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[0.20]),
        _bar(exit_date, tickers=("AAA",), mu=[-0.05], fwd_return=[0.0]),
    ]
    conv = ReplayConventions(stateful=True, tax=True, initial_capital=10_000.0)
    res = replay_one_allocator("s", alloc, bars, conv)
    return res.tax_paid[1]


class TestTaxLotConvention:
    def test_short_term_exit_charged_at_50_pct(self):
        # 2025-01-02 → 2025-12-31 = 363 days < 365 → short 50%.
        # Gain: 5000 × 0.20 = 1000 → tax 500.
        assert _tax_boundary_run("2025-12-31") == pytest.approx(500.0, abs=1e-9)

    def test_long_term_exit_charged_at_32_pct(self):
        # 2025-01-02 → 2026-01-02 = 365 days ≥ 365 → long 32% → tax 320.
        assert _tax_boundary_run("2026-01-02") == pytest.approx(320.0, abs=1e-9)

    def test_loss_exit_charges_zero_tax(self):
        alloc = _scripted_allocator([{"AAA": 0.50}, {}])
        bars = [
            _bar("2025-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[-0.20]),
            _bar("2025-06-01", tickers=("AAA",), mu=[-0.05], fwd_return=[0.0]),
        ]
        conv = ReplayConventions(stateful=True, tax=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.tax_paid[1] == pytest.approx(0.0, abs=1e-12)

    def test_no_tax_key_when_tax_disabled(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50}})
        bars = [_bar("2026-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[0.0])]
        res = replay_one_allocator(
            "s", alloc, bars, ReplayConventions(stateful=True),
        )
        assert res.tax_paid is None
        assert "tax_paid" not in res.to_dict()


# ── 4. Whole-share quantization ─────────────────────────────────────


class TestIntegerShares:
    def test_floor_conversion_and_residual_reported(self):
        # PV 10000, target 50% of AAA at price 333 → floor(5000/333)=15
        # shares = 4995 executed → E_executed 0.4995, residual 0.0005.
        # cap=0.5 blocks the one-share rescue (15+1 shares = 0.5328 >
        # cap), isolating the round-DOWN default.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[333.0], cap=0.50),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.executed_exposure[0] == pytest.approx(0.4995, abs=1e-12)
        assert res.integer_residual[0] == pytest.approx(0.0005, abs=1e-12)
        assert res.rescue_buys == [0]
        assert res.recheck_capdowns == [0]
        st = res.final_state
        assert st.position_shares("AAA") == pytest.approx(15.0, abs=1e-12)
        assert st.position_value("AAA") == pytest.approx(4995.0, abs=1e-9)
        d = res.to_dict()
        assert d["E_executed"] == [pytest.approx(0.4995)]
        assert d["integer_residual"] == [pytest.approx(0.0005)]
        assert d["total_rescue_buys"] == 0

    def test_executed_never_above_cap_post_round(self):
        """Executed-state invariant: with the per-name cap enforced, the
        executed weight can never exceed the cap post-round — floor-only
        fills, cap-bounded rescue, and post-round recheck together."""
        rng = np.random.default_rng(7)
        alloc = _fixed_target_allocator(
            {"*": {"AAA": 0.40, "BBB": 0.30, "CCC": 0.20}}
        )
        tickers = ("AAA", "BBB", "CCC")
        bars = [
            _bar(f"2026-01-{d:02d}", tickers=tickers,
                 mu=[0.05, 0.04, 0.03],
                 fwd_return=rng.normal(0.0, 0.02, 3),
                 prices=[97.0, 41.0, 13.0])
            for d in range(2, 12)
        ]
        conv = ReplayConventions(
            stateful=True, integer_shares=True, enforce_caps=True,
            per_name_cap=0.12, allow_unmapped_sectors=True,
        )
        res = replay_one_allocator("s", alloc, bars, conv)
        for e, r in zip(res.executed_exposure, res.integer_residual):
            assert e <= 3 * 0.12 + 1e-6
            # A rescue may overshoot a name's target by at most one
            # share (never its cap), so the residual can go marginally
            # negative — bounded by the cap tolerance, nothing more.
            assert r >= -1e-5
        # The post-round book NEVER exceeds the cap at execution time
        # (final-state check allows only post-execution return drift).
        st = res.final_state
        pv = st.portfolio_value
        for tk in tickers:
            assert st.position_value(tk) / pv <= 0.12 * 1.05, tk

    def test_shares_are_integral_and_carry_into_state(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50, "BBB": 0.30}})
        bars = [
            _bar(f"2026-01-{d:02d}", tickers=("AAA", "BBB"),
                 mu=[0.05, 0.04], fwd_return=[0.01, -0.01],
                 prices=[151.0, 47.0])
            for d in range(2, 8)
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        st = res.final_state
        for tk in ("AAA", "BBB"):
            shares = st.position_shares(tk)
            assert shares == pytest.approx(round(shares), abs=1e-6), tk

    def test_post_round_weights_carry_into_next_session(self):
        """The executed (post-round) weights — not the continuous
        targets — are what the next session's allocator sees."""
        seen: list[float] = []

        def spy(snap, *, mu, sigma=None):  # noqa: ARG001
            seen.append(float(snap.w_current[0]))
            target = np.array([0.50])
            return AllocatorResult(
                delta_w=target - snap.w_current,
                target_w=target,
                status="optimal",
                selected_indices=(0,),
            )

        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[333.0]),
            _bar("2026-01-03", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[333.0]),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        replay_one_allocator("spy", spy, bars, conv)
        # floor(5000/333)=15 shares, then the deferred rescue adds one
        # (still short of target, one share fits cap 1.0 + headroom):
        # 16 × 333 / 10000 = 0.5328 is the EXECUTED weight that carries.
        assert seen[1] == pytest.approx(0.5328, abs=1e-12)

    def test_missing_price_fails_loud(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.50}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05], fwd_return=[0.0]),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        with pytest.raises(ValueError, match="positive close price"):
            replay_one_allocator("s", alloc, bars, conv)


# ── 4b. RFC #443 §2.3 L3 — deferred rescue + post-round rechecks ────


class TestL3DeferredRescue:
    """The one-share rescue fires ONLY under the deferred-rescue
    conditions (post-round-down, cap- and reserve-headroom-bounded)."""

    def test_floored_to_zero_candidate_rescued_to_exactly_one_share(self):
        # target 0.008 · PV 10000 = $80 < 1 share ($100) → floor 0.
        # Rescue: one share = 1% PV ≤ cap AND ≤ headroom → exactly 1.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.008}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[100.0]),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.rescue_buys == [1]
        assert res.final_state.position_shares("AAA") == pytest.approx(1.0)
        assert res.executed_exposure[0] == pytest.approx(0.01, abs=1e-12)

    def test_rescue_blocked_by_per_name_cap(self):
        # One share = 1% PV > cap 0.9% → rescue may NOT round up.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.008}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[100.0], cap=0.009),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.rescue_buys == [0]
        assert res.final_state.position_shares("AAA") == pytest.approx(0.0)
        assert res.executed_exposure[0] == pytest.approx(0.0, abs=1e-12)

    def test_rescue_blocked_by_reserve_headroom(self):
        # Investable headroom = cash − reserve×PV = 10000 − 9950 = $50
        # < 1 share ($100) → no rescue (task_selection convention).
        alloc = _fixed_target_allocator({"*": {"AAA": 0.008}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[100.0], cash_reserve=0.995),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.rescue_buys == [0]
        assert res.final_state.position_shares("AAA") == pytest.approx(0.0)

    def test_rescue_spends_leftover_cash_in_conviction_order(self):
        # Headroom after round-down fits only TWO rescue shares; the two
        # highest-conviction (μ̂/σ̂²) names get them, the third does not.
        alloc = _fixed_target_allocator(
            {"*": {"AAA": 0.008, "BBB": 0.008, "CCC": 0.008}}
        )
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB", "CCC"),
                 mu=[0.02, 0.05, 0.04],       # conviction: BBB > CCC > AAA
                 fwd_return=[0.0, 0.0, 0.0],
                 prices=[100.0, 100.0, 100.0], cash_reserve=0.975),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        st = res.final_state
        assert res.rescue_buys == [2]
        assert st.position_shares("BBB") == pytest.approx(1.0)
        assert st.position_shares("CCC") == pytest.approx(1.0)
        assert st.position_shares("AAA") == pytest.approx(0.0)

    def test_deployment_not_understated_at_small_pv(self):
        """Hand-computed small-PV case: naive floor executes NOTHING
        (every target < 1 share), understating deployment to 0; the
        deferred rescue deploys 3 × $301 = $903 of $1000 → 0.903."""
        alloc = _fixed_target_allocator(
            {"*": {"AAA": 0.30, "BBB": 0.30, "CCC": 0.30}}
        )
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB", "CCC"),
                 mu=[0.05, 0.04, 0.03], fwd_return=[0.0, 0.0, 0.0],
                 prices=[301.0, 301.0, 301.0]),
        ]
        conv = ReplayConventions(
            stateful=True, integer_shares=True, initial_capital=1_000.0,
        )
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.rescue_buys == [3]
        assert res.executed_exposure[0] == pytest.approx(0.903, abs=1e-12)
        assert res.deployed_fraction[0] == pytest.approx(0.903, abs=1e-12)
        # Cash conservation with the rescue engaged stays exact.
        assert res.final_state.cash == pytest.approx(97.0, abs=1e-9)


class TestL3PostRoundRecheck:
    """RFC #443 §2.3: cash/name/sector/corr re-verified on EXECUTED
    quantities; violating buys are capped down — never carried in
    breach."""

    def test_name_cap_breach_capped_down(self):
        # Allocator proposes 0.30 with hard cap 0.20 (no enforce_caps —
        # the recheck itself must catch it): 30 shares round down, 10
        # are removed → executed exactly at the cap.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.30}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[100.0], cap=0.20),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.recheck_capdowns == [10]
        assert res.executed_exposure[0] == pytest.approx(0.20, abs=1e-12)
        assert res.final_state.position_shares("AAA") == pytest.approx(20.0)
        # Never carried in breach → no w_upper_hard violation recorded.
        assert res.violations_per_family.get("w_upper_hard", 0) == 0

    def test_sector_cap_breach_capped_down_lowest_conviction_first(self):
        # Both names in one sector capped at 0.25; targets 0.15 + 0.15.
        # Post-round load 0.30 → 5 shares of the LOWER-conviction name
        # (BBB, smaller μ̂) are removed → load exactly 0.25.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.15, "BBB": 0.15}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.06, 0.04],
                 fwd_return=[0.0, 0.0], prices=[100.0, 100.0],
                 sector_indicator=[[1.0, 1.0]], sector_cap_vec=[0.25]),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        st = res.final_state
        assert res.recheck_capdowns == [5]
        assert st.position_shares("AAA") == pytest.approx(15.0)
        assert st.position_shares("BBB") == pytest.approx(10.0)
        assert res.executed_exposure[0] == pytest.approx(0.25, abs=1e-12)
        assert res.violations_per_family.get("sector_cap", 0) == 0

    def test_corr_pair_breach_capped_down(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.15, "BBB": 0.15}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.06, 0.04],
                 fwd_return=[0.0, 0.0], prices=[100.0, 100.0],
                 corr_group_pairs=((0, 1, 0.25),)),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        st = res.final_state
        assert res.recheck_capdowns == [5]
        assert st.position_shares("AAA") + st.position_shares("BBB") == (
            pytest.approx(25.0)
        )
        # Lower-conviction BBB absorbed every removal.
        assert st.position_shares("BBB") == pytest.approx(10.0)
        assert res.violations_per_family.get("corr_group_cap", 0) == 0

    def test_cash_reserve_never_breached_by_fills(self):
        # Reserve 50% of PV: fills are headroom-bounded, so executed
        # exposure stops at 0.50 and cash never dips below the reserve.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.80}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0], prices=[100.0], cash_reserve=0.50),
        ]
        conv = ReplayConventions(stateful=True, integer_shares=True)
        res = replay_one_allocator("s", alloc, bars, conv)
        assert res.executed_exposure[0] == pytest.approx(0.50, abs=1e-12)
        assert res.final_state.cash == pytest.approx(5_000.0, abs=1e-9)
        assert res.rescue_buys == [0]


# ── 5. In-arm cap enforcement ───────────────────────────────────────


class TestCapEnforcement:
    def test_sector_projection_down_only(self):
        conv = ReplayConventions(
            enforce_caps=True,
            per_name_cap=0.30,
            sector_cap=0.35,
            sector_map={"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"},
        )
        target = np.array([0.25, 0.15, 0.10])   # Tech = 0.40 > 0.35
        proj, n_name, n_sector = apply_d6_cap_projection(
            target, ("AAA", "BBB", "CCC"), conv,
        )
        assert n_name == 0
        assert n_sector == 1
        # Down-only: nothing increased.
        assert (proj <= target + 1e-12).all()
        # Sector load exactly at the cap, proportional split preserved.
        assert proj[0] + proj[1] == pytest.approx(0.35, abs=1e-12)
        assert proj[0] / proj[1] == pytest.approx(0.25 / 0.15, rel=1e-9)
        # Out-of-sector name untouched.
        assert proj[2] == pytest.approx(0.10, abs=1e-12)

    def test_per_name_cap_clip_and_counter(self):
        conv = ReplayConventions(
            enforce_caps=True, per_name_cap=0.12,
            allow_unmapped_sectors=True,   # name-cap-only = exploratory
        )
        target = np.array([0.30, 0.10])
        proj, n_name, n_sector = apply_d6_cap_projection(
            target, ("AAA", "BBB"), conv,
        )
        assert n_name == 1 and n_sector == 0
        assert proj[0] == pytest.approx(0.12, abs=1e-12)
        assert proj[1] == pytest.approx(0.10, abs=1e-12)

    def test_unmapped_ticker_unconstrained_on_exploratory_path_only(self):
        # The permissive semantics (unmapped ⇒ no sector constraint, no
        # silent membership guessing) survive ONLY behind the explicit
        # exploratory flag; without it this exact call fails closed
        # (TestSectorMapFailClosed).
        conv = ReplayConventions(
            enforce_caps=True, per_name_cap=0.50, sector_cap=0.35,
            sector_map={"AAA": "Tech"}, allow_unmapped_sectors=True,
        )
        target = np.array([0.30, 0.45])
        proj, _, n_sector = apply_d6_cap_projection(
            target, ("AAA", "ZZZ"), conv,
        )
        assert n_sector == 0
        assert proj[1] == pytest.approx(0.45, abs=1e-12)

    def test_stateless_enforcement_prices_returns_on_projected_weights(self):
        """#445 gap 4: the cap is applied INSIDE the arm — the projected
        weights are what earns returns and pays costs, and the breach is
        counted per session instead of silently allowed."""
        alloc = _fixed_target_allocator({"*": {"AAA": 0.40, "BBB": 0.10}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.05, 0.04],
                 fwd_return=[0.10, 0.10], cost_bps=10.0),
        ]
        conv = ReplayConventions(
            enforce_caps=True, per_name_cap=0.12, allow_unmapped_sectors=True,
        )
        res = replay_one_allocator("s", alloc, bars, conv)
        # projected: [0.12, 0.10] → gross 0.022, turn 0.22, cost 0.00022
        assert res.daily_returns_net[0] == pytest.approx(
            0.022 - 0.22 * 10.0 * 1e-4, abs=1e-12,
        )
        assert res.turnover[0] == pytest.approx(0.22, abs=1e-12)
        assert res.name_cap_breaches == [1]
        assert res.sector_cap_breaches == [0]
        d = res.to_dict()
        assert d["total_name_cap_breaches"] == 1
        assert d["name_cap_breaches"] == [1]

    def test_stateful_enforcement_records_breaches(self):
        alloc = _fixed_target_allocator(
            {"*": {"AAA": 0.30, "BBB": 0.20, "CCC": 0.10}}
        )
        bars = [
            _bar(f"2026-01-{d:02d}", tickers=("AAA", "BBB", "CCC"),
                 mu=[0.05, 0.04, 0.03], fwd_return=[0.0, 0.0, 0.0])
            for d in range(2, 5)
        ]
        conv = ReplayConventions(
            stateful=True, enforce_caps=True,
            per_name_cap=0.12, sector_cap=0.20,
            sector_map={"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"},
        )
        res = replay_one_allocator("s", alloc, bars, conv)
        # Session 1 proposes over-cap names (2 breaches) + Tech over
        # sector cap post name-clip (0.24 > 0.20 → 1 breach). Later
        # sessions re-propose the same raw target → same counters.
        assert res.name_cap_breaches == [2, 2, 2]
        assert res.sector_cap_breaches == [1, 1, 1]
        # Executed book honours both caps.
        st = res.final_state
        pv = st.portfolio_value
        tech = (st.position_value("AAA") + st.position_value("BBB")) / pv
        assert tech <= 0.20 + 1e-9


# ── 5b. r2: sector-map fail-closed + execution-fidelity contract ────


class TestSectorMapFailClosed:
    """r2 #180: --enforce-caps must FAIL CLOSED when the sector map does
    not cover every active ticker in every replay bar; the permissive
    path survives only behind the explicit exploratory flag."""

    def test_no_map_at_all_fails_closed_at_replay_time(self):
        # Construction is allowed (callers need the object to run the
        # sector_map_coverage_gap prescan) — the replay itself raises.
        alloc = _fixed_target_allocator({"*": {"AAA": 0.10}})
        bars = [
            _bar("2026-01-02", tickers=("AAA",), mu=[0.05],
                 fwd_return=[0.0]),
        ]
        conv = ReplayConventions(enforce_caps=True)
        with pytest.raises(ValueError, match="does not cover active"):
            replay_one_allocator("s", alloc, bars, conv)

    def test_mixed_mapped_unmapped_universe_fails_closed_stateless(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.10, "ZZZ": 0.10}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "ZZZ"), mu=[0.05, 0.04],
                 fwd_return=[0.0, 0.0]),
        ]
        conv = ReplayConventions(
            enforce_caps=True, sector_map={"AAA": "Tech"},
        )
        with pytest.raises(ValueError, match="does not cover active"):
            replay_one_allocator("s", alloc, bars, conv)

    def test_mixed_mapped_unmapped_universe_fails_closed_stateful(self):
        alloc = _fixed_target_allocator({"*": {"AAA": 0.10, "ZZZ": 0.10}})
        bars = [
            _bar("2026-01-02", tickers=("AAA", "ZZZ"), mu=[0.05, 0.04],
                 fwd_return=[0.0, 0.0], prices=[100.0, 100.0]),
        ]
        conv = ReplayConventions(
            stateful=True, tax=True, integer_shares=True, enforce_caps=True,
            sector_map={"AAA": "Tech"},
        )
        with pytest.raises(ValueError, match="does not cover active"):
            replay_one_allocator("s", alloc, bars, conv)

    def test_exploratory_flag_allows_partial_map_and_marks_evidence(self):
        bars = build_default_fixture_bars()
        conv = ReplayConventions(
            stateful=True, enforce_caps=True,
            sector_map={"AAA": "Tech"},          # partial on purpose
            allow_unmapped_sectors=True,
        )
        payload = run_replay(
            bars, list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
            conventions=conv,
        )
        rc = payload["replay_conventions"]
        assert rc["sector_coverage"] == "exploratory_unmapped_allowed"
        assert rc["execution_fidelity"] == "L1_L2_ONLY"
        assert rc["promotion_eligible"] is False


class TestExecutionFidelityGate:
    """r2 #180 evidence contract: engaged-conventions payloads carry
    execution_fidelity/promotion_eligible, and the promotion gate
    REJECTS anything below L3_FULL — a floor-only / degraded result can
    never pass."""

    _FULL_MAP = {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy",
                 "DDD": "Energy"}

    def _payload(self, conv):
        bars = [
            dataclasses.replace(
                b, prices=np.array([120.0, 55.0, 250.0, 33.0]),
            )
            for b in build_default_fixture_bars()
        ]
        return run_replay(
            bars, list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
            conventions=conv,
        )

    def test_l3_full_stamp_when_all_conventions_engaged(self):
        conv = ReplayConventions(
            stateful=True, tax=True, integer_shares=True, enforce_caps=True,
            sector_map=self._FULL_MAP,
        )
        assert conv.execution_fidelity == "L3_FULL"
        payload = self._payload(conv)
        rc = payload["replay_conventions"]
        assert rc["execution_fidelity"] == "L3_FULL"
        assert rc["promotion_eligible"] is True
        assert rc["sector_coverage"] == "fail_closed"
        gate = payload["verdict"]["non_negotiable_gate_passed"]
        assert gate["execution_fidelity_l3_full"] is True

    def test_degraded_conventions_cannot_name_promotion_candidate(self):
        # Stateful-only (no tax, no integer shares, no cap enforcement)
        # = L1_L2_ONLY: the verdict fails closed with an explicit
        # fidelity rationale and every significance block is forced
        # diagnostic-only / non-promotable.
        conv = ReplayConventions(stateful=True)
        assert conv.execution_fidelity == "L1_L2_ONLY"
        payload = self._payload(conv)
        rc = payload["replay_conventions"]
        assert rc["execution_fidelity"] == "L1_L2_ONLY"
        assert rc["promotion_eligible"] is False
        verdict = payload["verdict"]
        assert verdict["promotion_candidate"] is None
        assert "L1_L2_ONLY" in verdict["rationale"]
        gate = verdict["non_negotiable_gate_passed"]
        assert gate["execution_fidelity_l3_full"] is False
        for name, sig in payload["significance"].items():
            assert sig["diagnostic_only"] is True, name
            assert sig["live_promotable_per_section_8"] is False, name
            assert "L3_FULL" in sig["promotion_block_reason"], name

    def test_gate_rejects_even_when_dsr_pbo_pass(self):
        """Negative proof at the gate level: a candidate whose DSR/PBO
        flags say 'promotable' and whose paired stats beat the incumbent
        still CANNOT pass without L3_FULL fidelity."""
        significance = {
            "incumbent_arm": {"dsr": 0.99, "pbo": 0.05, "pbo_se": 0.01,
                              "live_promotable_per_section_8": True},
            "challenger_arm": {"dsr": 0.99, "pbo": 0.05, "pbo_se": 0.01,
                               "live_promotable_per_section_8": True},
        }
        violations = {
            "any_allocator_violated_any_family": False,
            "by_allocator": {
                "incumbent_arm": {"rejected_for_promotion": False},
                "challenger_arm": {"rejected_for_promotion": False},
            },
        }
        paired = {
            "incumbent_arm_vs_challenger_arm": {
                "n_bars": 400,
                "delta_sharpe_annual": -2.0,      # challenger wins
                "win_rate_a_beats_b": 0.30,       # challenger wins 70%
            },
        }
        from renquant_pipeline.kernel.portfolio_qp.run_ab_replay import (
            apply_promotion_gate_to_significance,
            assemble_verdict,
        )
        # Sanity: WITH fidelity ok the challenger IS promotable…
        verdict_ok = assemble_verdict(
            significance, paired, violations, incumbent="incumbent_arm",
            execution_fidelity_ok=True,
        )
        assert verdict_ok["promotion_candidate"] == "challenger_arm"
        # …and WITHOUT L3_FULL fidelity it can NEVER be.
        verdict = assemble_verdict(
            significance, paired, violations, incumbent="incumbent_arm",
            execution_fidelity_ok=False,
        )
        assert verdict["promotion_candidate"] is None
        assert verdict["next_action"] == "iterate"
        assert "L1_L2_ONLY" in verdict["rationale"]
        gated = apply_promotion_gate_to_significance(
            significance, violations,
            constraints_decision_grade=True,
            execution_fidelity_ok=False,
        )
        for name, sig in gated.items():
            assert sig["live_promotable_per_section_8"] is False, name
            assert sig["diagnostic_only"] is True, name
            assert "L3_FULL" in sig["promotion_block_reason"], name


class TestSectorMapCoverageGap:
    """codex #180 review, 2026-07-10: apply_d6_cap_projection's per-bar
    unmapped-ticker-is-unconstrained behavior is correct at the math layer
    (test_unmapped_ticker_carries_no_sector_constraint above), but a CALLER
    running a D6-labeled sector-cap replay must not let that silently
    convert a missing hard constraint into no constraint across a whole
    run. sector_map_coverage_gap surfaces exactly which tickers a supplied
    map fails to cover, across ALL bars, so the caller can fail closed."""

    def test_full_coverage_is_empty_gap(self):
        conv = ReplayConventions(
            enforce_caps=True,
            sector_map={"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"},
        )
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.05, 0.04],
                 fwd_return=[0.0, 0.0]),
            _bar("2026-01-03", tickers=("BBB", "CCC"), mu=[0.04, 0.03],
                 fwd_return=[0.0, 0.0]),
        ]
        assert sector_map_coverage_gap(bars, conv) == ()

    def test_mixed_mapped_and_unmapped_tickers_across_bars(self):
        """Mixed coverage: AAA/BBB mapped, CCC/DDD not — DDD only appears
        in the second bar, proving the check scans EVERY bar, not just
        the first."""
        conv = ReplayConventions(
            enforce_caps=True, sector_map={"AAA": "Tech", "BBB": "Tech"},
        )
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB", "CCC"),
                 mu=[0.05, 0.04, 0.03], fwd_return=[0.0, 0.0, 0.0]),
            _bar("2026-01-03", tickers=("AAA", "DDD"),
                 mu=[0.05, 0.02], fwd_return=[0.0, 0.0]),
        ]
        gap = sector_map_coverage_gap(bars, conv)
        assert gap == ("CCC", "DDD")

    def test_no_map_at_all_treats_every_ticker_as_missing(self):
        conv = ReplayConventions(enforce_caps=True, sector_map=None)
        bars = [
            _bar("2026-01-02", tickers=("AAA", "BBB"), mu=[0.05, 0.04],
                 fwd_return=[0.0, 0.0]),
        ]
        assert sector_map_coverage_gap(bars, conv) == ("AAA", "BBB")


class TestExecutionFidelityPromotionGate:
    """codex #180 review, 2026-07-10: this harness is L1/L2-only (no
    deferred one-share rescue, no post-round executed-quantity cap/sector
    recheck — see doc/progress caveats). ANY engaged D6 convention must
    therefore stamp execution_fidelity=L1_L2_ONLY / promotion_eligible=
    False and the verdict/promotion path must reject it as decision-grade,
    even when every other gate (sector-map coverage, hard-constraint
    families) is fully satisfied."""

    def _decision_grade_bars(self, n=6):
        # snap must carry sector_indicator/sector_cap_vec so
        # constraint_fidelity_block's OWN check is satisfied — isolating
        # that the rejection below comes from execution_fidelity, not a
        # missing ConstraintSnapshot field.
        bars = []
        for d in range(2, 2 + n):
            snap = _snap(2, tickers=("AAA", "BBB"))
            snap = dataclasses.replace(
                snap,
                sector_indicator=np.array([[1.0, 1.0]]),
                sector_cap_vec=np.array([0.99]),
            )
            bars.append(AllocatorReplayBar(
                bar_date=f"2026-01-{d:02d}", snap=snap,
                mu=np.array([0.05, 0.03]), sigma=np.full(2, 0.10),
                fwd_return=np.array([0.01, 0.0]),
            ))
        return bars

    def test_floor_only_result_is_rejected_even_with_full_sector_coverage(self):
        bars = self._decision_grade_bars()
        conv = ReplayConventions(
            enforce_caps=True,
            sector_map={"AAA": "Tech", "BBB": "Tech"},  # full coverage
        )
        payload = run_replay(
            bars, ["equal_weight_top_k", "inverse_vol_top_k"],
            incumbent="equal_weight_top_k", conventions=conv,
        )
        assert payload["constraint_fidelity"]["execution_fidelity"] == "L1_L2_ONLY"
        assert payload["constraint_fidelity"]["promotion_eligible"] is False
        assert payload["constraint_fidelity"]["decision_grade"] is False
        assert payload["replay_conventions"]["execution_fidelity"] == "L1_L2_ONLY"
        assert payload["replay_conventions"]["promotion_eligible"] is False
        assert payload["verdict"]["promotion_candidate"] is None
        # Merged r1+r2 semantics: the verdict names the fidelity gap
        # explicitly (execution fidelity short-circuit) rather than the
        # generic constraint-fidelity wording.
        assert "L1_L2_ONLY" in payload["verdict"]["rationale"]
        for name, sig in payload["significance"].items():
            assert sig["live_promotable_per_section_8"] is False
            assert sig["diagnostic_only"] is True

    def test_no_conventions_engaged_stays_full_fidelity_and_promotable_schema(self):
        """Default mode (conventions=None) must NOT carry the new keys at
        all — additive-only, byte-identical to pre-D6 evidence."""
        bars = self._decision_grade_bars()
        payload = run_replay(
            bars, ["equal_weight_top_k", "inverse_vol_top_k"],
            incumbent="equal_weight_top_k", conventions=None,
        )
        assert "execution_fidelity" not in payload["constraint_fidelity"]
        assert "promotion_eligible" not in payload["constraint_fidelity"]
        assert "replay_conventions" not in payload


# ── 6/7. Evidence schema + CLI wiring ───────────────────────────────


def _write_cli_db(db_path: Path, *, with_prices: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE score_distribution (
            run_id TEXT, date TEXT, ticker TEXT, raw_panel REAL,
            rank_score REAL, mu REAL, sigma REAL, regime TEXT,
            is_holding INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE ticker_forward_returns (
            as_of_date TEXT, ticker TEXT, close_price REAL,
            fwd_1d REAL, fwd_5d REAL, fwd_10d REAL, fwd_20d REAL,
            fwd_60d REAL, updated_at TEXT
        )
        """
    )
    rng = np.random.default_rng(11)
    tickers = ["AAPL", "MSFT", "GOOG"]
    base_price = {"AAPL": 190.0, "MSFT": 420.0, "GOOG": 170.0}
    for day in range(1, 21):
        date = f"2024-02-{day:02d}"
        for rank, ticker in enumerate(tickers):
            mu = 0.03 - rank * 0.005 + float(rng.normal(0.0, 0.001))
            fwd = 0.004 - rank * 0.001 + float(rng.normal(0.0, 0.002))
            cur.execute(
                "INSERT INTO score_distribution "
                "(run_id, date, ticker, mu, sigma, regime) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("run-test", date, ticker, mu, 0.10 + rank * 0.02, "BULL_CALM"),
            )
            cur.execute(
                "INSERT INTO ticker_forward_returns "
                "(as_of_date, ticker, close_price, fwd_1d) "
                "VALUES (?, ?, ?, ?)",
                (
                    date, ticker,
                    base_price[ticker] if with_prices else None,
                    fwd,
                ),
            )
    conn.commit()
    conn.close()


class TestEvidenceSchemaAndCLI:
    def test_run_replay_conventions_block_and_per_allocator_keys(self):
        bars = [
            dataclasses.replace(
                b, prices=np.array([120.0, 55.0, 250.0, 33.0]),
            )
            for b in build_default_fixture_bars()
        ]
        conv = ReplayConventions(
            stateful=True, tax=True, integer_shares=True, enforce_caps=True,
            sector_map={"AAA": "Tech", "BBB": "Tech",
                        "CCC": "Energy", "DDD": "Energy"},
        )
        payload = run_replay(
            bars, list(FIXTURE_ALLOCATORS),
            incumbent=FIXTURE_INCUMBENT,
            pbo_n_slices=FIXTURE_PBO_N_SLICES,
            conventions=conv,
        )
        rc = payload["replay_conventions"]
        assert rc["stateful"] and rc["tax"]
        assert rc["integer_shares"] and rc["enforce_caps"]
        assert rc["tax_short_rate"] == 0.50
        assert rc["tax_long_rate"] == 0.32
        assert rc["execution_fidelity"] == "L3_FULL"
        assert rc["promotion_eligible"] is True
        assert rc["sector_coverage"] == "fail_closed"
        for name in FIXTURE_ALLOCATORS:
            block = payload["per_allocator"][name]
            assert len(block["deployed_fraction"]) == payload["n_bars"]
            assert len(block["E_executed"]) == payload["n_bars"]
            assert len(block["integer_residual"]) == payload["n_bars"]
            assert len(block["rescue_buys"]) == payload["n_bars"]
            assert len(block["recheck_capdowns"]) == payload["n_bars"]
            assert len(block["tax_paid"]) == payload["n_bars"]
            assert len(block["cost_paid"]) == payload["n_bars"]
            assert len(block["name_cap_breaches"]) == payload["n_bars"]
            assert len(block["sector_cap_breaches"]) == payload["n_bars"]
            assert "off_universe_liquidations" in block
        # Existing keys untouched.
        for key in (
            "sharpe_annual", "mean_daily_return", "cumulative_return",
            "max_drawdown", "mean_turnover", "cap_violations",
            "violations_per_family", "total_violations",
        ):
            assert key in payload["per_allocator"][FIXTURE_INCUMBENT]
        # JSON-serialisable end to end.
        json.dumps(payload)

    def test_cli_d6_flags_end_to_end(self, tmp_path):
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        sector_map = tmp_path / "sectors.json"
        sector_map.write_text(json.dumps(
            {"AAPL": "Tech", "MSFT": "Tech", "GOOG": "Comm"}
        ))
        out = tmp_path / "evidence.json"
        rc = main([
            "--wf-artifact-root", str(db),
            "--start-cut", "2024-02-01",
            "--end-cut", "2024-02-20",
            "--out", str(out),
            "--allocators", "equal_weight_top_k,inverse_vol_top_k",
            "--incumbent", "equal_weight_top_k",
            "--fwd-horizon-days", "1",
            "--pbo-n-slices", "4",
            "--stateful", "--tax", "--integer-shares", "--enforce-caps",
            "--sector-map-json", str(sector_map),
            "--cost-bps", "5",
            "--initial-capital", "25000",
        ])
        assert rc == 0
        payload = json.loads(out.read_text())
        rc_block = payload["replay_conventions"]
        assert rc_block["stateful"] is True
        assert rc_block["integer_shares"] is True
        assert rc_block["initial_capital"] == 25000.0
        assert rc_block["n_sector_mapped_tickers"] == 3
        assert rc_block["execution_fidelity"] == "L3_FULL"
        assert rc_block["promotion_eligible"] is True
        eq = payload["per_allocator"]["equal_weight_top_k"]
        assert len(eq["deployed_fraction"]) == payload["n_bars"]
        assert eq["total_cost_paid"] >= 0.0
        assert "E_executed" in eq and "integer_residual" in eq

    def test_cli_enforce_caps_partial_sector_map_fails_closed(self, tmp_path):
        """codex #180 review: a sector map covering only SOME of the
        active tickers (GOOG missing here) must fail closed by default —
        not silently proceed with GOOG unconstrained."""
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        sector_map = tmp_path / "sectors.json"
        sector_map.write_text(json.dumps({"AAPL": "Tech", "MSFT": "Tech"}))
        out = tmp_path / "evidence.json"
        rc = main([
            "--wf-artifact-root", str(db),
            "--start-cut", "2024-02-01",
            "--end-cut", "2024-02-20",
            "--out", str(out),
            "--allocators", "equal_weight_top_k,inverse_vol_top_k",
            "--incumbent", "equal_weight_top_k",
            "--fwd-horizon-days", "1",
            "--pbo-n-slices", "4",
            "--enforce-caps",
            "--sector-map-json", str(sector_map),
        ])
        assert rc == 2
        invalid = json.loads(out.read_text())
        assert invalid["invalid_experiment"] is True
        assert invalid["reason"] == "sector_map_incomplete"
        assert invalid["missing_sector_map_tickers"] == ["GOOG"]

    def test_cli_enforce_caps_no_sector_map_fails_closed(self, tmp_path):
        """No --sector-map-json at all with --enforce-caps must ALSO fail
        closed (every ticker is "missing"), not just warn as before."""
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        out = tmp_path / "evidence.json"
        rc = main([
            "--wf-artifact-root", str(db),
            "--start-cut", "2024-02-01",
            "--end-cut", "2024-02-20",
            "--out", str(out),
            "--allocators", "equal_weight_top_k,inverse_vol_top_k",
            "--incumbent", "equal_weight_top_k",
            "--fwd-horizon-days", "1",
            "--pbo-n-slices", "4",
            "--enforce-caps",
        ])
        assert rc == 2
        invalid = json.loads(out.read_text())
        assert invalid["invalid_experiment"] is True
        assert invalid["reason"] == "sector_map_incomplete"
        assert sorted(invalid["missing_sector_map_tickers"]) == [
            "AAPL", "GOOG", "MSFT",
        ]

    def test_cli_allow_partial_sector_map_escape_hatch(self, tmp_path):
        """--allow-partial-sector-map proceeds despite the gap, but the
        result is stamped non-decision-grade / non-promotable regardless
        (execution_fidelity is engaged-conventions-wide, not just a
        sector-map-coverage flag)."""
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        sector_map = tmp_path / "sectors.json"
        sector_map.write_text(json.dumps({"AAPL": "Tech", "MSFT": "Tech"}))
        out = tmp_path / "evidence.json"
        rc = main([
            "--wf-artifact-root", str(db),
            "--start-cut", "2024-02-01",
            "--end-cut", "2024-02-20",
            "--out", str(out),
            "--allocators", "equal_weight_top_k,inverse_vol_top_k",
            "--incumbent", "equal_weight_top_k",
            "--fwd-horizon-days", "1",
            "--pbo-n-slices", "4",
            "--enforce-caps",
            "--sector-map-json", str(sector_map),
            "--allow-partial-sector-map",
        ])
        assert rc == 0
        payload = json.loads(out.read_text())
        assert payload["constraint_fidelity"]["execution_fidelity"] == "L1_L2_ONLY"
        assert payload["constraint_fidelity"]["promotion_eligible"] is False
        assert payload["verdict"]["promotion_candidate"] is None
        # Mirrored stamps in replay_conventions (merged r1+r2 contract).
        rc_block = payload["replay_conventions"]
        assert rc_block["sector_coverage"] == "exploratory_unmapped_allowed"
        assert rc_block["execution_fidelity"] == "L1_L2_ONLY"
        assert rc_block["promotion_eligible"] is False
        assert "L1_L2_ONLY" in payload["verdict"]["rationale"]

    def test_cli_default_flags_unchanged_payload_schema(self, tmp_path):
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        out = tmp_path / "evidence.json"
        rc = main([
            "--wf-artifact-root", str(db),
            "--start-cut", "2024-02-01",
            "--end-cut", "2024-02-20",
            "--out", str(out),
            "--allocators", "equal_weight_top_k,inverse_vol_top_k",
            "--incumbent", "equal_weight_top_k",
            "--fwd-horizon-days", "1",
            "--pbo-n-slices", "4",
        ])
        assert rc == 0
        payload = json.loads(out.read_text())
        assert "replay_conventions" not in payload
        assert "deployed_fraction" not in payload["per_allocator"]["equal_weight_top_k"]

    def test_cli_tax_without_stateful_errors(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main([
                "--wf-artifact-root", str(tmp_path / "x.db"),
                "--start-cut", "2024-02-01",
                "--end-cut", "2024-02-20",
                "--out", str(tmp_path / "o.json"),
                "--tax",
            ])
        assert exc.value.code == 2

    def test_cli_integer_shares_without_stateful_errors(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            main([
                "--wf-artifact-root", str(tmp_path / "x.db"),
                "--start-cut", "2024-02-01",
                "--end-cut", "2024-02-20",
                "--out", str(tmp_path / "o.json"),
                "--integer-shares",
            ])
        assert exc.value.code == 2

    def test_cli_cost_bps_restamps_bars(self, tmp_path):
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        payloads = {}
        for bps in ("0", "100"):
            out = tmp_path / f"evidence-{bps}.json"
            rc = main([
                "--wf-artifact-root", str(db),
                "--start-cut", "2024-02-01",
                "--end-cut", "2024-02-20",
                "--out", str(out),
                "--allocators", "equal_weight_top_k",
                "--incumbent", "equal_weight_top_k",
                "--fwd-horizon-days", "1",
                "--pbo-n-slices", "4",
                "--cost-bps", bps,
            ])
            assert rc == 0
            payloads[bps] = json.loads(out.read_text())
        r0 = payloads["0"]["per_allocator"]["equal_weight_top_k"]
        r100 = payloads["100"]["per_allocator"]["equal_weight_top_k"]
        assert r0["mean_daily_return"] > r100["mean_daily_return"]

    def test_loader_stamps_close_prices(self, tmp_path):
        from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
            load_replay_bars_from_sim_db,
        )
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db)
        bars = load_replay_bars_from_sim_db(
            db, "2024-02-01", "2024-02-20", fwd_horizon_days=1,
        )
        assert bars
        for bar in bars:
            assert bar.prices is not None
            assert bar.prices.shape == (bar.snap.n,)
            assert np.isfinite(bar.prices).all()

    def test_loader_null_price_becomes_nan(self, tmp_path):
        from renquant_pipeline.kernel.portfolio_qp.wf_replay_loader import (
            load_replay_bars_from_sim_db,
        )
        db = tmp_path / "sim_runs.db"
        _write_cli_db(db, with_prices=False)
        bars = load_replay_bars_from_sim_db(
            db, "2024-02-01", "2024-02-20", fwd_horizon_days=1,
        )
        assert bars
        assert np.isnan(bars[0].prices).all()


# ── paired comparability in stateful mode ───────────────────────────


class TestStatefulPairedComparability:
    def test_replay_all_shares_bars_and_state_is_per_arm(self):
        """Each arm carries ITS OWN portfolio state; the shared input is
        the bar sequence, so paired daily returns stay well-defined."""
        bars = build_default_fixture_bars()
        conv = ReplayConventions(stateful=True)
        results = replay_all(
            {"eq": equal_weight_top_k, "eq2": equal_weight_top_k}, bars, conv,
        )
        assert results["eq"].bars == results["eq2"].bars == len(bars)
        # Identical allocator + identical per-arm fresh state → identical
        # series (no cross-arm state leakage).
        assert results["eq"].daily_returns_net == results["eq2"].daily_returns_net
        assert results["eq"].deployed_fraction == results["eq2"].deployed_fraction


class TestPortfolioStateHelpers:
    def test_empty_state_portfolio_value_is_cash(self):
        st = PortfolioState(cash=123.0)
        assert st.portfolio_value == 123.0
        assert st.total_positions_value() == 0.0
        assert st.position_value("X") == 0.0
        assert st.position_shares("X") == 0.0
