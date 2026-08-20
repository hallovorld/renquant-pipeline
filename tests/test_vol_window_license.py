"""Vol-window license (orch#1004 impl PR 1) — mechanism + capital-path tests.

Four contract families:

1. WINDOW COMPUTATION — the certified vol20 construction (close-to-close
   simple returns, sample std ddof=1, annualized sqrt(252) [VERIFIED — prior
   work, orch#1003 runner ``realized_vol20``]), the STRICT 0.135 boundary
   (exactly-at-threshold is OFF), BEAR precedence (hard override absolute),
   and PIT no-lookahead (the value at a session reads only trailing data).
2. UNREACHABILITY — for any config that does not explicitly enable
   ``ranking.panel_scoring.vol_window_license``, the enabled-path code is
   provably never executed (a raising stub does not fire) and the admission
   task's full decision surface is byte-identical to a frozen verbatim copy
   of the pre-change implementation (origin/main 763542b).
3. PROD BYTE-IDENTITY — the pre/post decision-state comparison across a grid
   of prod-shaped scenarios (refusal, admitted regime, admission disabled,
   missing scorer, holdings-only), serialized to canonical JSON bytes.
4. LICENSED-TOP-DECILE-ONLY — when the lane flag is on and the window is ON,
   exactly the top-decile (by served panel score) candidates survive; all
   other candidates/holdings get the pre-existing block-path treatment;
   governance refusals (diagnostic-only) are never overridden; the kill
   switch forces inactive; the session JSONL row is emitted.
"""
from __future__ import annotations

import datetime
import json
import math
from types import SimpleNamespace

import pytest

import renquant_pipeline.kernel.panel_pipeline.job_panel_scoring as jps
import renquant_pipeline.kernel.panel_pipeline.vol_window_license as vwl
from renquant_pipeline.context import InferenceContext

TODAY = datetime.date(2026, 8, 18)

# ── fixtures ──────────────────────────────────────────────────────────────────

# 30-name scored cross-section → top decile N = int(round(30/10)) = 3.
UNIVERSE = [f"T{i:02d}" for i in range(30)]
SCORES = {t: 1.0 - 0.01 * i for i, t in enumerate(UNIVERSE)}
TOP3 = ["T00", "T01", "T02"]

# ±2% alternating daily returns → vol20 ≈ 0.02·sqrt(20/19)·sqrt(252) ≈ 0.326
HIGH_VOL_RETURNS = [0.02 if i % 2 == 0 else -0.02 for i in range(60)]
# ±0.1% alternating → vol20 ≈ 0.016 — far below 0.135
LOW_VOL_RETURNS = [0.001 if i % 2 == 0 else -0.001 for i in range(60)]


def _refusing_metadata() -> dict:
    """BULL_CALM lacks WF evidence → regime_admission:failed:BULL_CALM."""
    return {
        "wf_gate_metadata": {
            "diagnostic_only": False,
            "passed": True,
            "trade_monotonicity": {
                "regimes": [
                    {"regime": "BULL_CALM", "eligible": True, "passed": False},
                ],
            },
            "sanity_regime_ic": {
                "regimes": {
                    "BULL_CALM": {"eligible": True, "passed": False,
                                  "mean_ic": 0.017},
                },
            },
        },
    }


def _admitting_bear_metadata() -> dict:
    return {
        "wf_gate_metadata": {
            "diagnostic_only": False,
            "passed": True,
            "trade_monotonicity": {
                "regimes": [
                    {"regime": "BEAR", "eligible": True, "passed": True},
                ],
            },
            "sanity_regime_ic": {
                "regimes": {
                    "BEAR": {"eligible": True, "passed": True,
                             "mean_ic": 0.27, "placebo_60_ic": 0.001},
                },
            },
        },
    }


def _cand(ticker: str) -> SimpleNamespace:
    return SimpleNamespace(ticker=ticker, expected_return=None)


def _ctx(
    *,
    metadata: dict | None = None,
    regime: str = "BULL_CALM",
    hard_bear: bool = False,
    spy_returns: list | None = None,
    candidates: list[str] | None = None,
    holdings: list[str] | None = None,
    admission_cfg: dict | None = None,
    license_cfg: dict | None = None,
    strategy_dir: str | None = None,
) -> InferenceContext:
    panel_scoring: dict = {
        "enabled": True,
        "regime_admission": (
            admission_cfg if admission_cfg is not None else {"enabled": True}
        ),
        "conviction_gate": {"enabled": False},
    }
    if license_cfg is not None:
        panel_scoring["vol_window_license"] = license_cfg
    config = {
        "watchlist": list(UNIVERSE),
        "ranking": {
            "panel_scoring": panel_scoring,
            "bull_calm_momentum_guard": {"enabled": False},
        },
    }
    if strategy_dir is not None:
        config["_strategy_dir"] = strategy_dir
    ctx = InferenceContext(config=config, today=TODAY)
    ctx.regime = regime
    ctx.regime_state = SimpleNamespace(hard_bear=hard_bear)
    ctx.spy_returns = list(
        spy_returns if spy_returns is not None else HIGH_VOL_RETURNS
    )
    ctx._panel_scorer = SimpleNamespace(
        metadata=metadata if metadata is not None else _refusing_metadata(),
    )
    ctx._panel_scores_all = dict(SCORES)
    ctx.candidates = [
        _cand(t)
        for t in (candidates if candidates is not None
                  else ["T00", "T02", "T10", "T17", "T25"])
    ]
    ctx.holdings = {
        t: SimpleNamespace(ticker=t, shares=1.0)
        for t in (holdings if holdings is not None else ["T01", "T20"])
    }
    return ctx


def _decision_state(ctx) -> bytes:
    """Canonical serialization of the ADMISSION decision surface.

    Excludes the license's own observability counters (``vol_window_ledger_*``
    — new sink bookkeeping, asserted separately) so the comparison isolates
    DECISIONS: candidates, blocked map, exit-only set/reasons, the admission
    record, funnel counters, and the cross-sectional snapshot.
    """
    counters = {
        k: v for k, v in (getattr(ctx, "counters", {}) or {}).items()
        if not str(k).startswith("vol_window_ledger")
    }
    return json.dumps({
        "candidates": [c.ticker for c in (ctx.candidates or [])],
        "blocked": getattr(ctx, "_blocked_by_ticker", None),
        "exit_only": sorted(getattr(ctx, "_qp_exit_only_tickers", set()) or set()),
        "exit_only_reasons": getattr(ctx, "_qp_exit_only_reasons", None),
        "admission": getattr(ctx, "_regime_model_admission", None),
        "counters": counters,
        "snapshot": [
            c.ticker
            for c in (getattr(ctx, "_full_candidate_snapshot", None) or [])
        ],
        "buy_blocked": getattr(ctx, "buy_blocked", None),
    }, sort_keys=True, default=str).encode("utf-8")


def _pre_change_reference_run(ctx) -> None:
    """VERBATIM copy of RegimeModelAdmissionTask.run at origin/main 763542b
    (pre vol-window change), delegating to the UNCHANGED module helpers.
    This is the byte-identity oracle: any behavioral drift the new code
    introduces for a config in this grid shows up as a byte diff."""
    candidates = list(getattr(ctx, "candidates", []) or [])
    holdings = getattr(ctx, "holdings", {}) or {}
    if not candidates and not holdings:
        return None
    panel_cfg = ctx.config.get("ranking", {}).get("panel_scoring", {})
    cfg = panel_cfg.get("regime_admission", {}) or {}
    scorer = getattr(ctx, "_panel_scorer", None)
    metadata = getattr(scorer, "metadata", {}) or {}
    regime = str(getattr(ctx, "regime", "") or "UNKNOWN")

    ok, reason, details = jps._diagnostic_only_admission(
        metadata, ctx.config, today=getattr(ctx, "today", None),
    )
    diagnostic_override = details.get("diagnostic_only_override")
    wf_fail_override = None
    if ok:
        wf_ok, wf_reason, wf_details = jps._wf_fail_admission(
            metadata, ctx.config, today=getattr(ctx, "today", None),
        )
        wf_fail_override = wf_details.get("wf_fail_override")
        if (not wf_ok) or (wf_fail_override is not None):
            ok, reason, details = wf_ok, wf_reason, wf_details
    override_records = {}
    if diagnostic_override:
        override_records["diagnostic_only_override"] = diagnostic_override
    if wf_fail_override:
        override_records["wf_fail_override"] = wf_fail_override
    if ok and cfg.get("enabled", True) is False:
        if override_records:
            ctx._regime_model_admission = {
                "ok": True, "reason": reason, "regime": regime,
                **details, **override_records,
            }
        return None
    if ok:
        ok, reason, details = jps._trade_monotonicity_admission(metadata, regime)
    if ok and bool(cfg.get("require_sanity_regime_ic", True)):
        ok, reason, details = jps._sanity_regime_admission(
            metadata,
            regime,
            min_ic=float(cfg.get("min_sanity_regime_ic", 0.02)),
            max_placebo_ratio=float(cfg.get("max_placebo_ratio", 0.5)),
        )
    ctx._regime_model_admission = {
        "ok": bool(ok), "reason": reason, "regime": regime, **details,
        **override_records,
    }
    if ok:
        return None

    ctx._full_candidate_snapshot = list(
        getattr(ctx, "_full_candidate_snapshot", None) or candidates
    )
    blocked = getattr(ctx, "_blocked_by_ticker", None) or {}
    for cand in candidates:
        blocked[cand.ticker] = reason
    if holdings:
        exit_only = set(getattr(ctx, "_qp_exit_only_tickers", set()) or set())
        exit_only_reasons = dict(getattr(ctx, "_qp_exit_only_reasons", {}) or {})
        for ticker in holdings:
            exit_only.add(ticker)
            exit_only_reasons.setdefault(ticker, reason)
            blocked.setdefault(ticker, reason)
        ctx._qp_exit_only_tickers = exit_only
        ctx._qp_exit_only_reasons = exit_only_reasons
    ctx._blocked_by_ticker = blocked
    n_candidates = len(candidates)
    n_holdings_exit_only = len(holdings) if holdings else 0
    ctx.candidates = []
    ctx.counters["regime_admission_blocked"] = (
        ctx.counters.get("regime_admission_blocked", 0) + n_candidates
    )
    ctx.counters["regime_admission_holdings_exit_only"] = (
        ctx.counters.get("regime_admission_holdings_exit_only", 0)
        + n_holdings_exit_only
    )


def _run_chain(ctx, runner) -> None:
    """A decision-run slice: admission + the unchanged downstream gates
    (both config-disabled, prod posture) so the surface feeds downstream
    exactly as in the real chain."""
    runner(ctx)
    jps.ConvictionGateTask().run(ctx)
    jps.BullCalmMomentumGuardTask().run(ctx)


# ── 1. window computation ─────────────────────────────────────────────────────

class TestWindowComputation:

    def test_matches_certified_construction(self):
        """vol20 == pct-change sample std (ddof=1) · sqrt(252) on the
        trailing 20 returns — the orch#1003 runner's realized_vol20 at the
        series tail."""
        rng = [((i * 2654435761) % 1000 - 500) / 25000.0 for i in range(40)]
        got = vwl.spy_realized_vol(rng, 20)
        tail = rng[-20:]
        mean = sum(tail) / 20
        expected = math.sqrt(
            sum((v - mean) ** 2 for v in tail) / 19
        ) * math.sqrt(252.0)
        assert got == pytest.approx(expected, rel=1e-12)

    def test_high_and_low_vol_fixtures_bracket_threshold(self):
        assert vwl.spy_realized_vol(HIGH_VOL_RETURNS, 20) > 0.135
        assert vwl.spy_realized_vol(LOW_VOL_RETURNS, 20) < 0.135

    def test_short_history_is_none(self):
        assert vwl.spy_realized_vol([0.01] * 19, 20) is None
        assert vwl.spy_realized_vol([], 20) is None
        assert vwl.spy_realized_vol(None, 20) is None

    def test_non_finite_input_is_none(self):
        assert vwl.spy_realized_vol([0.01] * 19 + [float("nan")], 20) is None
        assert vwl.spy_realized_vol([0.01] * 19 + [float("inf")], 20) is None

    def test_pit_no_lookahead(self):
        """The value at a session is a pure function of the trailing window:
        (a) only the last 20 observations are read; (b) appending FUTURE
        returns never changes a PAST session's value."""
        series = [((i * 40503) % 97 - 48) / 4000.0 for i in range(80)]
        # (a) trailing-window purity
        assert vwl.spy_realized_vol(series, 20) == vwl.spy_realized_vol(
            series[-20:], 20,
        )
        # (b) as-of stability under future data
        as_of_60 = vwl.spy_realized_vol(series[:60], 20)
        extended = series[:60] + [0.5, -0.5, 0.25]  # violent future
        assert vwl.spy_realized_vol(extended[:60], 20) == as_of_60

    def test_strict_threshold_exactly_at_is_off(self):
        """ON ⇔ vol20 > threshold, STRICT — a threshold set to the exactly
        computed vol20 must be OFF [orch#1001 §2: 'exactly 0.135 is OFF']."""
        vol = vwl.spy_realized_vol(HIGH_VOL_RETURNS, 20)
        ctx = _ctx(license_cfg={"enabled": True, "threshold": vol})
        record = vwl.evaluate_vol_window_license(
            ctx, ctx.config["ranking"]["panel_scoring"],
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        )
        assert record["vol_verdict_on"] is False
        assert record["license_applied"] is False
        # a hair below the computed vol → ON
        ctx2 = _ctx(license_cfg={"enabled": True, "threshold": vol - 1e-9})
        record2 = vwl.evaluate_vol_window_license(
            ctx2, ctx2.config["ranking"]["panel_scoring"],
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        )
        assert record2["vol_verdict_on"] is True
        assert record2["license_applied"] is True

    def test_boundary_at_the_frozen_constant(self, monkeypatch):
        monkeypatch.setattr(vwl, "spy_realized_vol", lambda *a, **k: 0.135)
        ctx = _ctx(license_cfg={"enabled": True})
        record = vwl.evaluate_vol_window_license(
            ctx, ctx.config["ranking"]["panel_scoring"],
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        )
        assert record["threshold"] == 0.135
        assert record["vol_verdict_on"] is False
        monkeypatch.setattr(
            vwl, "spy_realized_vol", lambda *a, **k: 0.1350000001,
        )
        record2 = vwl.evaluate_vol_window_license(
            ctx, ctx.config["ranking"]["panel_scoring"],
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        )
        assert record2["vol_verdict_on"] is True

    def test_bear_precedence_is_absolute(self):
        """regime==BEAR, hard_bear, and unresolved regimes each refuse the
        license even at extreme vol (design §2: absolute precedence;
        fail-closed narrowing for unresolved regimes)."""
        for kwargs in (
            {"regime": "BEAR"},
            {"regime": "BULL_CALM", "hard_bear": True},
            {"regime": "UNKNOWN"},
            {"regime": ""},
        ):
            ctx = _ctx(license_cfg={"enabled": True}, **kwargs)
            record = vwl.evaluate_vol_window_license(
                ctx, ctx.config["ranking"]["panel_scoring"],
                diagnostic_only_ok=True, admission_ok=False, base_reason="x",
            )
            assert record["vol_verdict_on"] is True, kwargs
            assert record["bear_precedence_blocked"] is True, kwargs
            assert record["window_on"] is False, kwargs
            assert record["license_applied"] is False, kwargs

    def test_missing_spy_history_fails_closed(self):
        ctx = _ctx(spy_returns=[0.01] * 5, license_cfg={"enabled": True})
        record = vwl.evaluate_vol_window_license(
            ctx, ctx.config["ranking"]["panel_scoring"],
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        )
        assert record["vol20"] is None
        assert record["license_applied"] is False


class TestTopDecile:

    def test_certified_count_and_membership(self):
        top, info = vwl.top_decile_by_score(SCORES)
        assert top == TOP3
        assert info["universe_n"] == 30
        assert info["top_decile_n"] == 3
        assert info["top_decile_score_floor"] == SCORES["T02"]

    def test_bankers_rounding_matches_runner(self):
        """N = int(round(n/10)) — Python banker's rounding, exactly the
        orch#1003 runner's construction: n=25 → 2, n=35 → 4."""
        top25, _ = vwl.top_decile_by_score(
            {f"A{i:02d}": -i for i in range(25)},
        )
        assert len(top25) == 2
        top35, _ = vwl.top_decile_by_score(
            {f"A{i:02d}": -i for i in range(35)},
        )
        assert len(top35) == 4

    def test_tiny_universe_yields_empty_decile(self):
        top, info = vwl.top_decile_by_score({"A": 1.0, "B": 0.5, "C": 0.1})
        assert top == []
        assert info["top_decile_n"] == 0

    def test_deterministic_tie_break_on_ticker(self):
        scores = {t: 1.0 for t in ("ZZ", "AA", "MM")} | {
            f"B{i:02d}": 0.0 for i in range(17)
        }
        top, _ = vwl.top_decile_by_score(scores)  # n=20 → N=2
        assert top == ["AA", "MM"]

    def test_non_finite_scores_excluded(self):
        scores = dict(SCORES)
        scores["T00"] = float("nan")
        top, info = vwl.top_decile_by_score(scores)
        assert "T00" not in top
        assert info["universe_n"] == 29


# ── 2/3. unreachability + prod byte-identity ─────────────────────────────────

NO_FLAG_GRID = [
    # (label, ctx-kwargs)
    ("bull_refusal", {}),
    ("admitted_bear", {
        "metadata": _admitting_bear_metadata(), "regime": "BEAR",
    }),
    ("admission_disabled_prod_posture", {
        "admission_cfg": {"enabled": False},
    }),
    ("missing_scorer_metadata", {"metadata": {}}),
    ("holdings_only", {"candidates": []}),
    ("flag_present_but_false", {"license_cfg": {"enabled": False}}),
    ("flag_present_but_string", {"license_cfg": {"enabled": "true"}}),
]


class TestUnreachableWithoutFlag:

    @pytest.mark.parametrize("label,kwargs", NO_FLAG_GRID)
    def test_enabled_path_never_executes(self, monkeypatch, label, kwargs):
        def _boom(*a, **k):
            raise AssertionError(
                "vol-window enabled path executed without the flag",
            )
        monkeypatch.setattr(vwl, "_evaluate_enabled", _boom)
        ctx = _ctx(**kwargs)
        _run_chain(ctx, jps.RegimeModelAdmissionTask().run)  # must not raise

    def test_evaluator_returns_none_without_touching_ctx(self):
        class _Tripwire:
            def __getattr__(self, name):  # pragma: no cover — trip only
                raise AssertionError(f"ctx attribute {name!r} read")
        assert vwl.evaluate_vol_window_license(
            _Tripwire(), {"regime_admission": {"enabled": True}},
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        ) is None
        assert vwl.evaluate_vol_window_license(
            _Tripwire(), {"vol_window_license": {"enabled": False}},
            diagnostic_only_ok=True, admission_ok=False, base_reason="x",
        ) is None

    def test_call_site_census(self):
        """The license integrates at exactly ONE site: the kernel
        RegimeModelAdmissionTask. A second consumer must rewrite this pin
        alongside its own review."""
        import pathlib

        import renquant_pipeline

        src_root = pathlib.Path(renquant_pipeline.__file__).parent
        importers = sorted(
            str(p.relative_to(src_root))
            for p in src_root.rglob("*.py")
            if "vol_window_license import" in p.read_text(encoding="utf-8")
        )
        assert importers == ["kernel/panel_pipeline/job_panel_scoring.py"]


class TestProdByteIdentity:

    @pytest.mark.parametrize("label,kwargs", NO_FLAG_GRID)
    def test_decision_surface_byte_identical(self, label, kwargs):
        ctx_new = _ctx(**kwargs)
        ctx_ref = _ctx(**kwargs)
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref), label

    def test_window_off_with_flag_on_is_decision_identical(self, tmp_path):
        """Design §1: out-of-window behavior is byte-identical even for the
        LANE (flag on, calm market) — the only delta is the lane's own
        session ledger row, which is not a decision."""
        license_cfg = {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        ctx_new = _ctx(spy_returns=LOW_VOL_RETURNS, license_cfg=license_cfg)
        ctx_ref = _ctx(spy_returns=LOW_VOL_RETURNS)
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref)
        rows = [
            json.loads(line)
            for line in (tmp_path / "ledger.jsonl").read_text().splitlines()
        ]
        assert len(rows) == 1
        assert rows[0]["window_on"] is False
        assert rows[0]["license_applied"] is False
        assert rows[0]["vol20"] < 0.135

    def test_bear_day_with_flag_on_is_decision_identical(self, tmp_path):
        """BEAR precedence at the task level: flag on + violent vol + BEAR
        regime ⇒ the full block path, byte-identical to pre-change."""
        license_cfg = {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        ctx_new = _ctx(regime="BEAR", license_cfg=license_cfg)
        ctx_ref = _ctx(regime="BEAR")
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref)
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["bear_precedence_blocked"] is True
        assert row["license_applied"] is False

    def test_diagnostic_only_refusal_never_licensed(self, tmp_path):
        """Governance precedence: diagnostic-only refusals are NOT the slot
        the license substitutes for — decisions stay byte-identical."""
        meta = {
            "wf_gate_metadata": {"diagnostic_only": True, "passed": True},
            "model_content_fingerprint_v1_recompute": "sha256:" + "ab" * 32,
        }
        license_cfg = {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        ctx_new = _ctx(metadata=meta, license_cfg=license_cfg)
        ctx_ref = _ctx(metadata=meta)
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref)
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["diagnostic_only_ok"] is False
        assert row["license_applied"] is False

    def test_kill_switch_forces_inactive(self, monkeypatch, tmp_path):
        monkeypatch.setenv(vwl.KILL_SWITCH_ENV, "1")
        license_cfg = {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        ctx_new = _ctx(license_cfg=license_cfg)
        ctx_ref = _ctx()
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref)
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["kill_switch"] is True
        assert row["license_applied"] is False

    def test_admission_disabled_with_flag_emits_note_row(self, tmp_path):
        """Misconfig visibility: license flag on while regime_admission is
        disabled → no refusal slot, decisions unchanged, honest note row."""
        license_cfg = {
            "enabled": True,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        ctx_new = _ctx(
            admission_cfg={"enabled": False}, license_cfg=license_cfg,
        )
        ctx_ref = _ctx(admission_cfg={"enabled": False})
        _run_chain(ctx_new, jps.RegimeModelAdmissionTask().run)
        _run_chain(ctx_ref, _pre_change_reference_run)
        assert _decision_state(ctx_new) == _decision_state(ctx_ref)
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["note"] == "regime_admission_disabled"
        assert row["license_applied"] is False


# ── 4. licensed-top-decile-only ───────────────────────────────────────────────

class TestLicenseApplied:

    def _licensed_ctx(self, tmp_path, **overrides):
        license_cfg = {
            "enabled": True,
            "threshold": 0.135,
            "vol_window_days": 20,
            "ledger_path": str(tmp_path / "ledger.jsonl"),
        }
        license_cfg.update(overrides.pop("license_overrides", {}))
        return _ctx(license_cfg=license_cfg, **overrides)

    def test_only_top_decile_candidates_survive(self, tmp_path):
        ctx = self._licensed_ctx(tmp_path)
        jps.RegimeModelAdmissionTask().run(ctx)
        # candidates were T00,T02 (top-decile) + T10,T17,T25 (not)
        assert [c.ticker for c in ctx.candidates] == ["T00", "T02"]
        blocked = ctx._blocked_by_ticker
        for t in ("T10", "T17", "T25"):
            assert blocked[t] == "regime_admission:failed:BULL_CALM"
        for t in ("T00", "T02"):
            assert t not in blocked

    def test_non_top_decile_holdings_exit_only_licensed_holding_not(
        self, tmp_path,
    ):
        ctx = self._licensed_ctx(tmp_path)  # holdings: T01 (top), T20 (not)
        jps.RegimeModelAdmissionTask().run(ctx)
        exit_only = ctx._qp_exit_only_tickers
        assert "T20" in exit_only
        assert "T01" not in exit_only
        assert ctx._qp_exit_only_reasons["T20"] == (
            "regime_admission:failed:BULL_CALM"
        )

    def test_admission_record_and_counters(self, tmp_path):
        ctx = self._licensed_ctx(tmp_path)
        jps.RegimeModelAdmissionTask().run(ctx)
        adm = ctx._regime_model_admission
        assert adm["ok"] is True
        assert adm["reason"] == "ok:vol_window_license"
        assert adm["underlying_reason"] == "regime_admission:failed:BULL_CALM"
        assert adm["vol_window_license"]["license_applied"] is True
        assert adm["vol_window_license"]["top_decile"] == TOP3
        assert ctx.counters["vol_window_license_sessions"] == 1
        assert ctx.counters["vol_window_licensed_candidates"] == 2
        assert ctx.counters["regime_admission_blocked"] == 3
        assert ctx.counters["regime_admission_holdings_exit_only"] == 1

    def test_full_candidate_snapshot_is_pre_partition(self, tmp_path):
        """Downstream cross-sectional references (ConvictionGate demean) must
        still see the FULL pre-license candidate list — same contract as the
        block path's snapshot."""
        ctx = self._licensed_ctx(tmp_path)
        jps.RegimeModelAdmissionTask().run(ctx)
        assert [c.ticker for c in ctx._full_candidate_snapshot] == [
            "T00", "T02", "T10", "T17", "T25",
        ]

    def test_session_row_carries_licensed_names(self, tmp_path):
        ctx = self._licensed_ctx(tmp_path)
        jps.RegimeModelAdmissionTask().run(ctx)
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["schema"] == vwl.SCHEMA_VERSION
        assert row["license_applied"] is True
        assert row["window_on"] is True
        assert row["vol20"] > 0.135
        assert row["threshold"] == 0.135
        assert row["hard_bear"] is False
        assert row["regime"] == "BULL_CALM"
        assert row["top_decile"] == TOP3
        assert row["licensed_candidates"] == ["T00", "T02"]
        assert row["licensed_holdings"] == ["T01"]
        assert row["base_reason"] == "regime_admission:failed:BULL_CALM"
        assert row["date"] == TODAY.isoformat()

    def test_no_top_decile_candidate_present_still_licenses_holdings(
        self, tmp_path,
    ):
        """The license only ADDS admissibility to names actually present —
        an empty intersection licenses nothing but stays honest."""
        ctx = self._licensed_ctx(
            tmp_path, candidates=["T10", "T17"], holdings=["T20"],
        )
        jps.RegimeModelAdmissionTask().run(ctx)
        assert ctx.candidates == []
        assert "T20" in ctx._qp_exit_only_tickers
        row = json.loads(
            (tmp_path / "ledger.jsonl").read_text().splitlines()[0],
        )
        assert row["license_applied"] is True
        assert row["licensed_candidates"] == []

    def test_empty_score_cross_section_fails_closed(self, tmp_path):
        ctx = self._licensed_ctx(tmp_path)
        ctx._panel_scores_all = {}
        ctx_ref = _ctx()
        jps.RegimeModelAdmissionTask().run(ctx)
        _pre_change_reference_run(ctx_ref)
        assert _decision_state(ctx) == _decision_state(ctx_ref)

    def test_default_ledger_path_under_strategy_dir(self, tmp_path):
        ctx = _ctx(
            license_cfg={"enabled": True},
            strategy_dir=str(tmp_path),
        )
        jps.RegimeModelAdmissionTask().run(ctx)
        path = tmp_path / "logs" / "vol_window_license.jsonl"
        assert path.exists()
        assert ctx.counters["vol_window_ledger_logged"] == 1

    def test_ledger_write_failure_never_flips_the_decision(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where a directory must go")
        ctx = self._licensed_ctx(
            tmp_path,
            license_overrides={
                # parent is a FILE → mkdir/open raises → swallowed + counted
                "ledger_path": str(blocker / "sub" / "ledger.jsonl"),
            },
        )
        jps.RegimeModelAdmissionTask().run(ctx)  # must not raise
        assert [c.ticker for c in ctx.candidates] == ["T00", "T02"]
        assert ctx.counters["vol_window_ledger_errors"] == 1

    def test_frozen_defaults_match_design_constants(self):
        """Runner-guards-are-prereg-content: the code defaults ARE the frozen
        design constants [orch#1001 §2 / #1004 §2]."""
        assert vwl.DEFAULT_ON_THRESHOLD == 0.135
        assert vwl.DEFAULT_VOL_WINDOW_DAYS == 20
        assert vwl.ANNUALIZATION_DAYS == 252.0
        assert vwl.TOP_DECILE_DIVISOR == 10
        assert vwl.KILL_SWITCH_ENV == "RENQUANT_VOL_WINDOW_LICENSE_DISABLE"


def test_evaluate_handles_pandas_series_scores(monkeypatch):
    """MAIDEN-SESSION regression (2026-08-18): ctx._panel_scores_all is a pandas
    Series in the real pipeline; the old ``or {}`` truthiness raised
    'truth value of a Series is ambiguous' and crashed the lane's first run."""
    import pandas as pd
    from renquant_pipeline.kernel.panel_pipeline import vol_window_license as vwl

    scores = pd.Series({"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "DDD": 4.0,
                        "EEE": 5.0, "FFF": 6.0, "GGG": 7.0, "HHH": 8.0,
                        "III": 9.0, "JJJ": 10.0})
    top, info = vwl.top_decile_by_score(scores)
    assert top == ["JJJ"]
    assert info["universe_n"] == 10

    # and the call-site path: a ctx whose scores are a Series must not raise
    class Ctx:
        _panel_scores_all = scores
        spy_returns = [0.001] * 50
        config = {}
    # evaluate under a disabled flag: must return None WITHOUT evaluating
    # truthiness on the Series (the crash happened before the flag check's
    # partition, at the top_decile call)
    rec = vwl.evaluate_vol_window_license(
        Ctx(), {"vol_window_license": {"enabled": True}},
        diagnostic_only_ok=True, admission_ok=False, base_reason="test",
    )
    # must not raise on the Series; returns a record (window state evaluated)
    assert rec is not None
