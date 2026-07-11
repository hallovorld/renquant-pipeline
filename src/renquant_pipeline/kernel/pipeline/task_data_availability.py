"""DataAvailabilityGateTask — pre-decision input availability & vintage gate.

Operator mandate (2026-07-11, after the META / 07-08 investigations): input-
integrity checking is FRAGMENTED — OHLCV staleness fail-closes per symbol
(``DataFreshnessGateTask``, the good pattern), admission-model metadata
staleness fail-closes per ticker (``job_universe.FilterStalenessTask``) but its
07-08 collapse went out as a normal no-trade, the fundamentals serving axis had
a preflight gate (P-FUND-FRESHNESS) that was structurally unsatisfiable for
~88 days without anyone noticing (the serving-axis clip bug), the panel model
artifact vintage was a SOFT-SKIP (P-MODEL-STALENESS — the 2026-06-26 incident:
a model trained to 2024-11 served silently), and NOTHING checked that a
required dataset exists at all (the SGOV case). This task is the one general,
meaningful gate: it verifies every declared INPUT AXIS early in the daily
inference pipeline, before any scoring or decision logic.

INPUT-SIDE COMPLEMENT of ``FunnelIntegrityTask`` (renquant-pipeline #186):
FunnelIntegrity classifies the OUTPUT funnel at the END of the run; this gate
verifies the INPUTS at the START. It never re-classifies outcomes — no overlap
in responsibility. The two blocks share the reporting plane (a schema-stamped
dict on the run context, mirrored into ``ctx.counters``, persisted verbatim
into the run bundle by downstream persistence).

WHAT IT VERIFIES, per axis: PRESENCE (the input exists at all), AS-OF VINTAGE
(the input's binding as-of date vs the axis's declared freshness budget), and
UNIVERSE COVERAGE (the fraction of the expected universe the input covers).

BUILT-IN AXES v1 (each checked with safe defaults even when undeclared):

  * ``ohlcv_bars``               — per expected symbol (watchlist + holdings +
    benchmark + sector ETFs, reusing ``DataFreshnessGateTask``'s expected-set
    logic): bar presence + calendar-day vintage + coverage. The session-aware
    fail-closed staleness enforcement REMAINS ``DataFreshnessGateTask`` —
    this axis is the contract-declared reporting/coverage view, not a
    replacement.
  * ``fundamentals_serving_axis`` — the live serving fundamentals feed
    (``data/sec_fundamentals_daily.parquet``): per-symbol as-of dates, global
    feed vintage, watchlist coverage. Catches the serving-axis-clip incident
    class (feed frozen ~88d while P-FUND-FRESHNESS was unsatisfiable; fixed
    base-data #26 + pipeline #151).
  * ``panel_model_artifact``     — file presence, fingerprint resolvable
    (stamped or recomputable via the SHARED ``renquant_common.model_fingerprint``
    impl — never a local re-fork), and train vintage (``trained_date`` +
    binding train-data cutoff via ``job_universe.TRAINING_DATA_FIELDS``) vs a
    declared max age. This makes the P-MODEL-STALENESS soft-skip a REAL
    config-keyed policy: ``fail_closed`` | ``degrade_with_alarm`` (default
    degrade-with-alarm so prod is not darked on day one).
  * ``calibrator``               — global calibration resolvable when required
    (the ``missing_global_calibration`` fail-close signature, caught BEFORE
    scoring), method/params sane, fit vintage when stamped. Fingerprint
    EQUALITY is deliberately NOT re-verified here (scoring fail-close owns it;
    the calibrator/scorer triple-impl bug is exactly what a fourth
    hand-copied comparison would recreate) — only stamp PRESENCE is reported.
  * ``admission_model_metadata`` — AGGREGATE coverage of the admitted
    per-ticker universe, REUSING ``job_universe._classify_cutoffs`` /
    ``_resolve_axes`` (the existing staleness gate's own classification —
    not a duplicate). Coverage collapse (the 07-08 signature: 133/145 stale →
    buy scan on ~0 tickers) fires here; floor default aligned with the
    umbrella #463 ``universe_collapse_floor_frac`` (0.5).
  * ``regime_inputs``            — benchmark (SPY) bar presence + vintage;
    ``spy_returns`` / GMM presence reported as evidence.
  * ``account_snapshot``         — portfolio/cash/holdings snapshot presence;
    as-of age when the adapter stamps ``ctx.account_snapshot_at`` (the missing
    stamp is surfaced as evidence so the provenance gap is visible, and a
    contract may set ``require_as_of`` once adapters stamp it).

CUSTOM AXES (the whole-dataset-absence class, e.g. SGOV): any extra entry
under ``data_contracts.axes`` with ``kind: dataset_file | dataset_dir`` and a
``path`` (absolute, or relative to the resolved data root) is verified for
presence, optional vintage (``date_column`` + ``max_staleness_days`` on a
parquet), and optional sealed-manifest fingerprint presence (``manifest``
path — the base-data crypto_bars / D-C2 ingestion-manifest pattern).

CONTRACTS ARE DECLARED, NOT HARDCODED (``config["data_contracts"]``, schema
``data_contracts.v1`` — the shape mirrors renquant-base-data's dataset
manifests: dataset/axis id + freshness rule + how it is validated):

    "data_contracts": {
      "schema": "data_contracts.v1",
      "axes": {
        "fundamentals_serving_axis": {"max_staleness_days": 20,
                                       "min_coverage": 0.80,
                                       "policy": "degrade_with_alarm"},
        "panel_model_artifact":      {"max_train_age_days": 120,
                                       "max_cutoff_age_days": 335,
                                       "policy": "fail_closed"},
        "sleeve_sgov_bars":          {"kind": "dataset_file",
                                       "path": "data/sleeve/SGOV.parquet",
                                       "date_column": "date",
                                       "max_staleness_days": 5}
      }
    }

A consumed built-in axis with NO declared contract is still checked with the
defaults, and the gate warns LOUDLY (``missing_contracts`` in the block) so
new inputs get contracts.

FAIL POLICY, honoured per axis (``policy`` in the axis contract):

  * ``fail_closed``         — a violated (or unverifiable) axis ABORTS the run
    loudly, exactly like ``DataFreshnessGateTask`` today. Fail-isolated
    construction does NOT apply: if the checker for a fail-closed axis
    crashes, the input cannot be verified and the gate refuses to proceed.
  * ``degrade_with_alarm``  — the DAY-ONE DEFAULT for every axis: the run
    proceeds; the alarm lands in ``ctx.data_availability`` (run bundle) +
    ``ctx.counters`` and is ntfy-visible via :func:`notification_fields`
    (same stamping pattern as the umbrella #463 ``universe_health`` /
    ``universe_collapse`` fields — this module only EMITS the fields; the
    umbrella wires them). Checker crashes are fail-isolated here.

OUTPUT CONTRACT (``ctx.data_availability``, schema ``data_availability.v1``):

  ``schema, date, run_mode, verdict (AVAILABLE|DEGRADED|BLOCKED), degraded,
  blocked, axes{name: {verdict, policy, contract_declared, present, as_of,
  age_days, coverage, n_have, n_expected, violations[], evidence, error,
  contract}}, fired[] (axis / policy / reason / evidence),
  axes_evaluated[], missing_contracts[], error``

Counters mirror: ``data_availability_fired`` / ``data_availability_degraded``
/ ``data_availability_blocked`` / ``data_availability_errors``.

CONTRACT (hard): OBSERVE/VERIFY ONLY — no signal, decision, sizing or order
state is mutated; ZERO decision-logic change. The only behavioural effect is
the abort for axes an operator has EXPLICITLY opted into ``fail_closed``
(none by default). Kill switch: ``data_availability.enabled = false``.
Deliberately NOT in SellOnlyPipeline: this is buy-input verification; the
sell path keeps its own ``DataFreshnessGateTask`` and must never be blocked
by a buy-side input alarm (same reasoning as P-FUND-FRESHNESS sell-only
exemption and FunnelIntegrityTask's sell-only skip).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline._data_root import data_root

log = logging.getLogger("kernel.pipeline.data_availability")

SCHEMA_VERSION = "data_availability.v1"
CONTRACTS_SCHEMA_VERSION = "data_contracts.v1"
CTX_ATTR = "data_availability"

POLICY_FAIL_CLOSED = "fail_closed"
POLICY_DEGRADE = "degrade_with_alarm"
_KNOWN_POLICIES = (POLICY_FAIL_CLOSED, POLICY_DEGRADE)

AXIS_OK = "ok"
AXIS_VIOLATION = "violation"
AXIS_SKIP = "skip"
AXIS_ERROR = "error"

VERDICT_AVAILABLE = "AVAILABLE"
VERDICT_DEGRADED = "DEGRADED"
VERDICT_BLOCKED = "BLOCKED"

# ── Default contracts (used when an axis is consumed but undeclared) ─────────
#
# Budgets deliberately ALIGN with the existing fragmented checks they unify,
# so day-one behaviour is warning-parity, not a new opinion:
#   ohlcv_bars.max_staleness_days=5        calendar-day slack (weekend+holiday);
#                                          session-aware enforcement stays
#                                          DataFreshnessGateTask.
#   fundamentals.max_staleness_days=20     == DataVerificationTask fundamentals
#                                          max_stale_days == P-FUND-FRESHNESS
#                                          max_feed_stale_days.
#   panel_model.max_train_age_days=120     == P-MODEL-STALENESS quarterly rail.
#   panel_model.max_cutoff_age_days=335    == P-MODEL-STALENESS decay-curve knee.
#   admission.min_coverage=0.5             == umbrella #463
#                                          universe_collapse_floor_frac default.
DEFAULT_CONTRACTS: dict[str, dict[str, Any]] = {
    "ohlcv_bars": {"max_staleness_days": 5, "min_coverage": 1.0},
    "fundamentals_serving_axis": {"max_staleness_days": 20, "min_coverage": 0.80},
    "panel_model_artifact": {"max_train_age_days": 120, "max_cutoff_age_days": 335},
    "calibrator": {},
    "admission_model_metadata": {"min_coverage": 0.5},
    "regime_inputs": {"max_staleness_days": 5},
    "account_snapshot": {"max_staleness_minutes": 24 * 60},
}

_CUSTOM_DATASET_KINDS = ("dataset_file", "dataset_dir")


# ── Axis result ───────────────────────────────────────────────────────────────

@dataclass
class AxisResult:
    """One input axis's verification outcome (pre-policy)."""

    axis: str
    verdict: str = AXIS_OK
    present: bool | None = None
    as_of: str | None = None                 # binding (worst) as-of date, ISO
    age_days: int | None = None
    coverage: float | None = None
    n_have: int | None = None
    n_expected: int | None = None
    violations: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    error: str | None = None
    # Filled by the task from the effective contract:
    policy: str = POLICY_DEGRADE
    contract_declared: bool = False
    contract: dict = field(default_factory=dict)

    def finalize(self) -> "AxisResult":
        if self.error is not None:
            self.verdict = AXIS_ERROR
        elif self.violations:
            self.verdict = AXIS_VIOLATION
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "policy": self.policy,
            "contract_declared": self.contract_declared,
            "present": self.present,
            "as_of": self.as_of,
            "age_days": self.age_days,
            "coverage": self.coverage,
            "n_have": self.n_have,
            "n_expected": self.n_expected,
            "violations": list(self.violations),
            "evidence": dict(self.evidence),
            "error": self.error,
            "contract": dict(self.contract),
        }


# ── Small tolerant helpers ────────────────────────────────────────────────────

def _config(ctx: Any) -> dict:
    cfg = getattr(ctx, "config", None)
    if isinstance(cfg, dict):
        return cfg
    cfg = getattr(ctx, "strategy_config", None)
    return cfg if isinstance(cfg, dict) else {}


def _session_date(ctx: Any) -> _dt.date:
    today = getattr(ctx, "today", None)
    if isinstance(today, _dt.datetime):
        return today.date()
    if isinstance(today, _dt.date):
        return today
    if today is not None:
        try:
            return pd.to_datetime(today).date()
        except Exception:  # noqa: BLE001
            pass
    return _dt.date.today()


def _parse_date(raw: Any) -> _dt.date | None:
    if raw is None:
        return None
    try:
        return _dt.date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _max_bar_date(df: Any) -> _dt.date | None:
    if df is None or len(df) == 0:
        return None
    try:
        if isinstance(df.index, pd.DatetimeIndex):
            return df.index.max().date()
        return pd.to_datetime(df.index.max()).date()
    except Exception:  # noqa: BLE001
        return None


def _resolve_data_path(raw: str) -> "tuple[Path | None, str | None]":
    """(path, skip_note) — absolute as-is; relative joins the data root."""
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p, None
    try:
        return Path(data_root()) / p, None
    except Exception as exc:  # noqa: BLE001
        return None, f"data_root unresolvable ({exc}) — cannot locate {raw!r}"


def _panel_cfg(ctx: Any) -> "tuple[dict, dict]":
    """(panel_scoring cfg, owning config dict) — kernel or runtime ctx shape."""
    for attr in ("config", "strategy_config"):
        cfg = getattr(ctx, attr, None)
        if isinstance(cfg, dict):
            pc = (cfg.get("ranking") or {}).get("panel_scoring") or {}
            if pc:
                return pc, cfg
    return {}, _config(ctx)


def _artifact_metadata(path: Path, kind: "str | None") -> "dict | None":
    """Artifact metadata dict: JSON artifact, else the sequence sidecar."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    except Exception:  # noqa: BLE001 — binary checkpoint / non-JSON: try sidecar
        pass
    try:
        from renquant_pipeline.kernel.preflight import (  # noqa: PLC0415
            _load_sequence_sidecar,
        )
        meta, _source = _load_sequence_sidecar(path)
        if isinstance(meta, dict):
            return meta
    except Exception:  # noqa: BLE001
        pass
    return None


# ── Built-in axis checkers ───────────────────────────────────────────────────
# Each checker: (ctx, effective_contract) → AxisResult. Checkers only READ ctx.

def check_ohlcv_bars(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("ohlcv_bars")
    cfg = _config(ctx)
    # Reuse DataFreshnessGateTask's expected-universe derivation (watchlist +
    # holdings + benchmark + sector ETFs, sell-only aware) — one definition of
    # "expected symbols", not a duplicate.
    from .task_data_freshness import DataFreshnessGateTask  # noqa: PLC0415
    expected = sorted(
        DataFreshnessGateTask._expected_symbols(  # noqa: SLF001 — deliberate reuse
            ctx, cfg.get("data_freshness") or {},
        )
    )
    if not expected:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = "no expected symbols (empty watchlist/holdings)"
        return r

    ohlcv = getattr(ctx, "ohlcv", None) or {}
    r.present = bool(ohlcv)
    r.n_expected = len(expected)
    max_stale = int(contract.get("max_staleness_days", 5))
    min_cov = float(contract.get("min_coverage", 1.0))
    today = _session_date(ctx)

    missing: list[str] = []
    stale: list[tuple[str, str, int]] = []
    oldest: _dt.date | None = None
    n_fresh = 0
    for sym in expected:
        d = _max_bar_date(ohlcv.get(sym))
        if d is None:
            missing.append(sym)
            continue
        if oldest is None or d < oldest:
            oldest = d
        age = (today - d).days
        if age > max_stale:
            stale.append((sym, d.isoformat(), age))
        else:
            n_fresh += 1
    r.n_have = n_fresh
    r.coverage = n_fresh / len(expected)
    if oldest is not None:
        r.as_of = oldest.isoformat()
        r.age_days = (today - oldest).days
    if not ohlcv:
        r.violations.append("ohlcv_absent")
    if missing:
        r.violations.append(f"bars_missing:{len(missing)}/{len(expected)}")
        r.evidence["missing_sample"] = missing[:10]
    if stale:
        r.violations.append(
            f"bars_stale:{len(stale)}/{len(expected)} beyond {max_stale}d"
        )
        r.evidence["stale_sample"] = [
            f"{s}@{d}({a}d)" for s, d, a in stale[:10]
        ]
    if r.coverage < min_cov:
        r.violations.append(
            f"coverage {r.coverage:.2f} < min_coverage {min_cov:.2f}"
        )
    return r


def check_fundamentals_serving_axis(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("fundamentals_serving_axis")
    rel = str(contract.get("path", "data/sec_fundamentals_daily.parquet"))
    path, skip_note = _resolve_data_path(rel)
    if path is None:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = skip_note
        return r
    r.evidence["path"] = str(path)
    if not path.exists():
        r.present = False
        r.violations.append(f"dataset_missing:{path}")
        return r
    r.present = True

    try:
        try:
            df = pd.read_parquet(path, columns=["date", "ticker"])
        except Exception:  # noqa: BLE001 — column subset unsupported / renamed
            df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        r.violations.append(f"dataset_unreadable:{type(exc).__name__}")
        return r
    if "date" not in df.columns:
        r.violations.append("as_of_column_missing:date")
        return r

    today = _session_date(ctx)
    max_stale = int(contract.get("max_staleness_days", 20))
    min_cov = float(contract.get("min_coverage", 0.80))
    dates = pd.to_datetime(df["date"], errors="coerce")
    global_max = dates.max()
    if pd.isna(global_max):
        r.violations.append("as_of_dates_unparseable")
        return r
    r.as_of = global_max.date().isoformat()
    r.age_days = (today - global_max.date()).days
    if r.age_days > max_stale:
        r.violations.append(
            f"serving_axis_stale:{r.age_days}d > {max_stale}d "
            f"(as-of {r.as_of}) — the serving feed is not advancing"
        )

    wl = [str(t) for t in (_config(ctx).get("watchlist") or [])]
    if wl and "ticker" in df.columns:
        per_ticker = (
            df.assign(_d=dates).groupby("ticker")["_d"].max()
        )
        fresh: set[str] = set()
        stale_sample: list[str] = []
        for t in wl:
            md = per_ticker.get(t)
            if md is None or pd.isna(md):
                continue
            age = (today - md.date()).days
            if age <= max_stale:
                fresh.add(t)
            elif len(stale_sample) < 10:
                stale_sample.append(f"{t}@{md.date().isoformat()}({age}d)")
        r.n_expected = len(wl)
        r.n_have = len(fresh)
        r.coverage = len(fresh) / len(wl)
        if stale_sample:
            r.evidence["stale_symbol_sample"] = stale_sample
        if r.coverage < min_cov:
            r.violations.append(
                f"coverage {r.coverage:.2f} < min_coverage {min_cov:.2f} "
                f"({r.n_have}/{r.n_expected} within {max_stale}d)"
            )
    return r


def check_panel_model_artifact(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("panel_model_artifact")
    pc, cfg = _panel_cfg(ctx)
    if not pc:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = "panel scoring not configured"
        return r
    if pc.get("enabled", True) is False:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = "panel scoring disabled"
        return r
    manifest = getattr(ctx, "artifact_manifest", None) or {}
    rel = pc.get("artifact_path") or manifest.get("local_artifact_path")
    if not rel:
        r.present = False
        r.violations.append("artifact_path_undeclared")
        return r
    p = Path(str(rel))
    if not p.is_absolute():
        strategy_dir = cfg.get("_strategy_dir")
        if strategy_dir:
            p = Path(strategy_dir) / p
    r.evidence["path"] = str(p)
    r.evidence["kind"] = pc.get("kind")

    if not p.exists() or (p.is_file() and p.stat().st_size <= 0):
        r.present = False
        r.violations.append(f"artifact_missing:{p}")
        return r
    r.present = True

    meta = _artifact_metadata(p, pc.get("kind"))
    if meta is None:
        r.violations.append("artifact_metadata_unreadable")
        return r

    today = _session_date(ctx)
    max_train_age = int(contract.get("max_train_age_days", 120))
    max_cutoff_age = int(contract.get("max_cutoff_age_days", 335))

    trained = _parse_date(meta.get("trained_date") or meta.get("trained_at"))
    if trained is None:
        # Mirrors ModelStalenessTask: an unmeasurable model age is a
        # provenance gap, never a pass.
        r.violations.append("trained_date_unstamped (model age unmeasurable)")
    else:
        r.evidence["trained_date"] = trained.isoformat()
        train_age = (today - trained).days
        r.evidence["train_age_days"] = train_age
        if train_age > max_train_age:
            r.violations.append(
                f"train_vintage_stale:{train_age}d > {max_train_age}d "
                f"(trained {trained.isoformat()})"
            )

    # Binding train-data cutoff — same alias axis the admission staleness gate
    # uses (job_universe.TRAINING_DATA_FIELDS), read via ITS reader.
    from .job_universe import TRAINING_DATA_FIELDS, _axis_cutoff  # noqa: PLC0415
    cutoff, cutoff_field, cutoff_present = _axis_cutoff(
        meta, TRAINING_DATA_FIELDS,
    )
    if not cutoff_present:
        if bool(contract.get("require_cutoff_stamp", True)):
            r.violations.append(
                "train_cutoff_unstamped (decay-curve rail unmeasurable — "
                "provenance gap, not a pass)"
            )
    elif cutoff is None:
        r.violations.append(f"train_cutoff_unparseable:{cutoff_field}")
    else:
        r.as_of = cutoff.isoformat()
        r.age_days = (today - cutoff).days
        r.evidence["cutoff_field"] = cutoff_field
        if r.age_days > max_cutoff_age:
            r.violations.append(
                f"train_cutoff_stale:{r.age_days}d > {max_cutoff_age}d "
                f"({cutoff_field}={cutoff.isoformat()}) — the 2026-06-26 "
                f"silent-vintage incident class"
            )
    if r.as_of is None and trained is not None:
        r.as_of = trained.isoformat()
        r.age_days = (today - trained).days

    # Fingerprint resolvable: a stamped digest, or a recompute through the
    # SHARED implementation. Equality vs the calibrator is NOT re-checked
    # here (scoring fail-close owns it).
    fp_source = None
    for key in ("model_content_sha256", "artifact_sha256", "content_sha256"):
        if meta.get(key):
            fp_source = f"stamped:{key}"
            break
    if fp_source is None:
        fp = str(manifest.get("feature_fingerprint") or "")
        if fp and not fp.startswith("legacy:"):
            fp_source = "manifest:feature_fingerprint"
    if fp_source is None:
        try:
            from renquant_common.model_fingerprint import (  # noqa: PLC0415
                model_content_sha256_from_path,
            )
            import warnings  # noqa: PLC0415

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                if model_content_sha256_from_path(p):
                    fp_source = "recomputed"
        except Exception:  # noqa: BLE001
            fp_source = None
    r.evidence["fingerprint_source"] = fp_source
    if fp_source is None:
        r.violations.append("fingerprint_unresolvable")
    return r


def check_calibrator(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("calibrator")
    pc, cfg = _panel_cfg(ctx)
    manifest = getattr(ctx, "artifact_manifest", None) or {}

    meta: dict = {}
    rel = pc.get("artifact_path") or manifest.get("local_artifact_path")
    if rel:
        p = Path(str(rel))
        if not p.is_absolute():
            strategy_dir = cfg.get("_strategy_dir")
            if strategy_dir:
                p = Path(strategy_dir) / p
        if p.exists():
            meta = _artifact_metadata(p, pc.get("kind")) or {}

    calibration: dict = {}
    for source in (
        pc.get("global_calibration"),
        meta.get("global_calibration"),
        meta.get("calibration"),
        (meta.get("metrics") or {}).get("global_calibration"),
        manifest.get("global_calibration"),
    ):
        if isinstance(source, dict) and source:
            calibration = source
            break
    required = bool(
        (pc.get("global_calibration") or {}).get("required")
        or calibration.get("required")
    )
    r.evidence["required"] = required
    if not calibration:
        if required:
            r.present = False
            r.violations.append(
                "calibrator_missing (scoring will fail closed: "
                "missing_global_calibration)"
            )
        else:
            r.verdict = AXIS_SKIP
            r.evidence["note"] = "no calibrator configured"
        return r

    r.present = True
    method = calibration.get("method")
    r.evidence["method"] = method
    if not method:
        if required:
            r.violations.append(
                "calibrator_method_missing (scoring will fail closed: "
                "missing_global_calibration)"
            )
        else:
            r.evidence["note"] = "method unset — calibration inert"
    else:
        for key in ("slope", "intercept"):
            if key in calibration:
                try:
                    value = float(calibration[key])
                except (TypeError, ValueError):
                    value = float("nan")
                if value != value:  # NaN
                    r.violations.append(f"calibrator_param_invalid:{key}")

    fitted = _parse_date(
        calibration.get("fitted_date")
        or calibration.get("calibrated_at")
        or calibration.get("fit_date")
    )
    if fitted is not None:
        r.as_of = fitted.isoformat()
        r.age_days = (_session_date(ctx) - fitted).days
        max_stale = contract.get("max_staleness_days")
        if max_stale is not None and r.age_days > int(max_stale):
            r.violations.append(
                f"calibrator_stale:{r.age_days}d > {int(max_stale)}d"
            )
    # Stamp PRESENCE only — never a fourth hand-rolled equality check.
    r.evidence["fingerprint_stamped"] = bool(
        calibration.get("model_content_sha256")
    )
    return r


def check_admission_model_metadata(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("admission_model_metadata")
    cfg = _config(ctx)
    wl = [str(t) for t in (cfg.get("watchlist") or [])]
    if not wl:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = "empty watchlist"
        return r
    models = getattr(ctx, "models", None) or {}
    r.present = bool(models)
    r.n_expected = len(wl)

    # REUSE the admission staleness gate's own classification — the aggregate
    # view of exactly what FilterStalenessTask computes per ticker.
    from .job_universe import (  # noqa: PLC0415
        TRAINING_DATA_FIELDS,
        _axis_cutoff,
        _classify_cutoffs,
        _resolve_axes,
    )
    staleness_days = int(cfg.get("model_staleness_days", 0) or 0)
    axes = _resolve_axes(cfg)
    today = _session_date(ctx)

    verdict_counts: dict[str, int] = {}
    oldest_cutoff: _dt.date | None = None
    n_fresh = 0
    for t in wl:
        art = models.get(t)
        if not isinstance(art, dict):
            verdict_counts["not_admitted"] = (
                verdict_counts.get("not_admitted", 0) + 1
            )
            continue
        if staleness_days > 0:
            meta = art.get("_metadata", {}) or {}
            verdict, field_name, age = _classify_cutoffs(
                meta, axes, today, staleness_days,
            )
            if verdict == "fresh":
                # Track the binding cutoff for the as-of report.
                cutoff, _f, _p = _axis_cutoff(meta, TRAINING_DATA_FIELDS)
                if cutoff is not None and (
                    oldest_cutoff is None or cutoff < oldest_cutoff
                ):
                    oldest_cutoff = cutoff
        else:
            verdict = "fresh"    # staleness gate disabled → admitted == covered
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        if verdict == "fresh":
            n_fresh += 1

    r.n_have = n_fresh
    r.coverage = n_fresh / len(wl)
    r.evidence["verdict_counts"] = verdict_counts
    r.evidence["model_staleness_days"] = staleness_days
    if oldest_cutoff is not None:
        r.as_of = oldest_cutoff.isoformat()
        r.age_days = (today - oldest_cutoff).days

    min_cov = float(contract.get("min_coverage", 0.5))
    if r.coverage < min_cov:
        r.violations.append(
            f"admission_coverage_collapse:{n_fresh}/{len(wl)} "
            f"({r.coverage:.2f} < min_coverage {min_cov:.2f}) — the 07-08 "
            f"incident signature (buy scan on a collapsed universe)"
        )
    return r


def check_regime_inputs(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("regime_inputs")
    cfg = _config(ctx)
    bench = str(cfg.get("benchmark", "SPY") or "SPY")
    r.evidence["benchmark"] = bench
    r.evidence["spy_returns_n"] = len(getattr(ctx, "spy_returns", None) or [])
    r.evidence["gmm_loaded"] = getattr(ctx, "gmm", None) is not None

    ohlcv = getattr(ctx, "ohlcv", None) or {}
    d = _max_bar_date(ohlcv.get(bench))
    r.present = d is not None
    r.n_expected = 1
    r.n_have = int(r.present)
    r.coverage = float(r.n_have)
    if d is None:
        r.violations.append(f"benchmark_bars_missing:{bench}")
        return r
    today = _session_date(ctx)
    r.as_of = d.isoformat()
    r.age_days = (today - d).days
    max_stale = int(contract.get("max_staleness_days", 5))
    if r.age_days > max_stale:
        r.violations.append(
            f"benchmark_stale:{bench}@{r.as_of} ({r.age_days}d > {max_stale}d)"
        )
    return r


def check_account_snapshot(ctx: Any, contract: dict) -> AxisResult:
    r = AxisResult("account_snapshot")
    pv = float(getattr(ctx, "portfolio_value", 0.0) or 0.0)
    cash = float(getattr(ctx, "cash", 0.0) or 0.0)
    holdings = getattr(ctx, "holdings", None) or {}
    r.present = pv > 0.0 or cash > 0.0 or bool(holdings)
    r.evidence["portfolio_value_present"] = pv > 0.0
    r.evidence["cash_present"] = cash > 0.0
    r.evidence["n_holdings"] = len(holdings)
    if not r.present:
        r.violations.append(
            "account_snapshot_absent (portfolio_value=0, cash=0, no holdings)"
        )

    stamp = None
    for attr in ("account_snapshot_at", "account_as_of", "account_refreshed_at"):
        raw = getattr(ctx, attr, None)
        if raw is not None:
            try:
                stamp = pd.Timestamp(raw)
            except Exception:  # noqa: BLE001
                stamp = None
            break
    if stamp is None:
        # Adapters do not stamp a snapshot time today — surface the gap
        # (evidence, not violation) so the contract can enforce it later.
        r.evidence["as_of_stamp"] = "unavailable"
        if bool(contract.get("require_as_of", False)):
            r.violations.append("account_as_of_unstamped")
        return r
    now = getattr(ctx, "run_timestamp", None)
    now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=stamp.tz)
    if stamp.tzinfo is None and now_ts.tzinfo is not None:
        now_ts = now_ts.tz_localize(None)
    elif stamp.tzinfo is not None and now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize(stamp.tz)
    age_minutes = max(0.0, (now_ts - stamp).total_seconds() / 60.0)
    r.as_of = stamp.isoformat()
    r.evidence["age_minutes"] = round(age_minutes, 1)
    max_minutes = int(contract.get("max_staleness_minutes", 24 * 60))
    if age_minutes > max_minutes:
        r.violations.append(
            f"account_snapshot_stale:{age_minutes:.0f}min > {max_minutes}min"
        )
    return r


def check_custom_dataset(ctx: Any, name: str, contract: dict) -> AxisResult:
    """Declared dataset axis — the whole-dataset-absence (SGOV) class."""
    r = AxisResult(name)
    kind = contract.get("kind")
    if kind not in _CUSTOM_DATASET_KINDS:
        r.violations.append(f"contract_invalid:unknown_kind:{kind}")
        return r
    raw = contract.get("path")
    if not raw:
        r.violations.append("contract_invalid:path_undeclared")
        return r
    path, skip_note = _resolve_data_path(str(raw))
    if path is None:
        r.verdict = AXIS_SKIP
        r.evidence["note"] = skip_note
        return r
    r.evidence["path"] = str(path)

    if kind == "dataset_dir":
        r.present = path.is_dir() and any(path.iterdir())
        if not r.present:
            r.violations.append(f"dataset_missing:{path}")
            return r
    else:
        r.present = path.is_file() and path.stat().st_size > 0
        if not r.present:
            r.violations.append(f"dataset_missing:{path}")
            return r

    max_stale = contract.get("max_staleness_days")
    date_column = contract.get("date_column")
    if kind == "dataset_file" and max_stale is not None and date_column:
        try:
            df = pd.read_parquet(path, columns=[str(date_column)])
            md = pd.to_datetime(df[str(date_column)], errors="coerce").max()
        except Exception as exc:  # noqa: BLE001
            r.violations.append(f"dataset_unreadable:{type(exc).__name__}")
            return r
        if pd.isna(md):
            r.violations.append(f"as_of_dates_unparseable:{date_column}")
            return r
        today = _session_date(ctx)
        r.as_of = md.date().isoformat()
        r.age_days = (today - md.date()).days
        if r.age_days > int(max_stale):
            r.violations.append(
                f"dataset_stale:{r.age_days}d > {int(max_stale)}d "
                f"(as-of {r.as_of})"
            )

    # Sealed ingestion-manifest fingerprint presence (base-data crypto_bars /
    # D-C2 pattern): the manifest must parse and carry a content fingerprint.
    manifest_rel = contract.get("manifest")
    if manifest_rel:
        mpath, mskip = _resolve_data_path(str(manifest_rel))
        if mpath is None:
            r.evidence["manifest_note"] = mskip
        elif not mpath.exists():
            r.violations.append(f"manifest_missing:{mpath}")
        else:
            try:
                mdoc = json.loads(mpath.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                mdoc = None
                r.violations.append(
                    f"manifest_unreadable:{type(exc).__name__}"
                )
            if isinstance(mdoc, dict):
                fp = (
                    mdoc.get("fingerprint")
                    or mdoc.get("content_sha256")
                    or mdoc.get("sha256")
                )
                r.evidence["manifest_fingerprint_present"] = bool(fp)
                if not fp:
                    r.violations.append("manifest_fingerprint_unresolvable")
    return r


BUILTIN_CHECKERS: "dict[str, Callable[[Any, dict], AxisResult]]" = {
    "ohlcv_bars": check_ohlcv_bars,
    "fundamentals_serving_axis": check_fundamentals_serving_axis,
    "panel_model_artifact": check_panel_model_artifact,
    "calibrator": check_calibrator,
    "admission_model_metadata": check_admission_model_metadata,
    "regime_inputs": check_regime_inputs,
    "account_snapshot": check_account_snapshot,
}


# ── Notification contract (umbrella #463 universe_health stamping pattern) ───

def notification_fields(block: "dict | None") -> dict[str, Any]:
    """ntfy-adapter for the umbrella: consumes ``ctx.data_availability``.

    Returns ``{"degraded": bool, "blocked": bool, "title_tag": str,
    "line": str}``. The umbrella stamps the block verbatim into the run
    bundle (``bundle["data_availability"]``) plus the top-level boolean
    (``bundle["data_availability_degraded"]``) exactly like #463 stamps
    ``universe_health`` + ``universe_collapse``. Emitted here; wired there.
    """
    if not isinstance(block, dict) or not block.get("verdict"):
        return {
            "degraded": False,
            "blocked": False,
            "title_tag": "UNKNOWN",
            "line": "data availability: not evaluated",
        }
    verdict = str(block["verdict"])
    tag = {
        VERDICT_BLOCKED: "DATA-BLOCKED",
        VERDICT_DEGRADED: "DATA-DEGRADED",
        VERDICT_AVAILABLE: "DATA-OK",
    }.get(verdict, "UNKNOWN")
    fired = block.get("fired") or []
    fired_names = ", ".join(
        str(f.get("axis", "?")) for f in fired if isinstance(f, dict)
    )
    line = f"data availability: {verdict}"
    if fired_names:
        line += f" [{fired_names}]"
    error = block.get("error")
    if error:
        line += f" (gate error: {error})"
    return {
        "degraded": bool(block.get("degraded")),
        "blocked": bool(block.get("blocked")),
        "title_tag": tag,
        "line": line,
    }


# ── The task ──────────────────────────────────────────────────────────────────

class DataAvailabilityGateTask:
    """Pre-decision input availability & vintage verification (module docstring).

    Verify-only. Aborts ONLY for axes an operator explicitly declared
    ``policy: fail_closed`` (none by default). Everything else degrades with
    an alarm in ``ctx.data_availability`` + counters.
    """

    def __init__(
        self,
        checkers: "dict[str, Callable[[Any, dict], AxisResult]] | None" = None,
    ) -> None:
        self._checkers = dict(checkers or BUILTIN_CHECKERS)

    def run(self, ctx: Any) -> bool:
        cfg = _config(ctx)
        da_cfg = cfg.get("data_availability") or {}
        if da_cfg.get("enabled", True) is False:
            log.info("DataAvailabilityGateTask: disabled via config — skipping")
            return True
        run_mode = str(getattr(ctx, "_run_mode", "") or "")
        if run_mode.strip().lower().replace("_", "-").startswith("sell-only"):
            return True    # buy-input gate; the sell path is never blocked here

        try:
            block, blocked = self._build_block(ctx, cfg)
            setattr(ctx, CTX_ATTR, block)
            self._mirror_counters(ctx, block)
        except Exception as exc:  # noqa: BLE001 — verify-only: fail-isolated…
            self._record_error(ctx, exc)
            if self._any_fail_closed_declared(cfg):
                # …EXCEPT when an operator demanded fail-closed verification:
                # an unverifiable input is a fail, not a pass.
                raise RuntimeError(
                    "DataAvailabilityGateTask: gate crashed while at least one "
                    "axis is declared policy=fail_closed — inputs cannot be "
                    f"verified; refusing to proceed ({type(exc).__name__}: {exc})"
                ) from exc
            return True

        fired = block["fired"]
        if fired:
            log.warning(
                "DataAvailabilityAlert: %s — %d axis(es) fired: %s "
                "(policies honoured per axis; see ctx.data_availability)",
                block["verdict"], len(fired),
                ", ".join(f["axis"] for f in fired),
            )
        else:
            log.info(
                "DataAvailabilityGateTask: PASS — %d axes verified (%s)",
                len(block["axes_evaluated"]),
                ", ".join(block["axes_evaluated"]),
            )
        if blocked:
            names = ", ".join(r.axis for r in blocked)
            reasons = "; ".join(
                f"{r.axis}: {'; '.join(r.violations) or r.error or r.verdict}"
                for r in blocked
            )
            msg = (
                f"DataAvailabilityGateTask: INPUT UNAVAILABLE — fail-closed "
                f"axis(es) violated: {names}. {reasons}. Refusing to proceed "
                f"(policy=fail_closed declared in data_contracts)."
            )
            log.error(msg)
            raise RuntimeError(msg)
        return True

    # Internal ---------------------------------------------------------------

    def _build_block(
        self, ctx: Any, cfg: dict,
    ) -> "tuple[dict[str, Any], list[AxisResult]]":
        contracts_cfg = cfg.get("data_contracts") or {}
        declared_axes = contracts_cfg.get("axes") or {}
        if contracts_cfg and contracts_cfg.get("schema") not in (
            None, CONTRACTS_SCHEMA_VERSION,
        ):
            log.warning(
                "data_contracts.schema=%r is not %r — reading it as v1; "
                "review the contracts section",
                contracts_cfg.get("schema"), CONTRACTS_SCHEMA_VERSION,
            )

        results: dict[str, AxisResult] = {}
        missing_contracts: list[str] = []

        for name, checker in self._checkers.items():
            declared = declared_axes.get(name)
            if declared is None:
                missing_contracts.append(name)
                declared = {}
            elif not isinstance(declared, dict):
                log.warning(
                    "data_contracts.axes[%r] is not a mapping — ignoring it",
                    name,
                )
                declared = {}
            contract = {**DEFAULT_CONTRACTS.get(name, {}), **declared}
            results[name] = self._run_checker(
                ctx, name, contract,
                declared_in_config=name not in missing_contracts,
                checker=checker,
            )

        # Custom declared axes (dataset presence contracts — the SGOV class).
        for name, declared in declared_axes.items():
            if name in self._checkers:
                continue
            contract = declared if isinstance(declared, dict) else {}
            results[name] = self._run_checker(
                ctx, name, dict(contract), declared_in_config=True,
                checker=lambda c, k, _n=name: check_custom_dataset(c, _n, k),
            )

        if missing_contracts:
            log.warning(
                "DataAvailabilityGateTask: NO DATA CONTRACT declared for "
                "consumed input axis(es): %s — checked with built-in defaults. "
                "Declare each under config.data_contracts.axes (schema %s) so "
                "freshness budgets / coverage floors / fail policies are "
                "explicit.",
                ", ".join(missing_contracts), CONTRACTS_SCHEMA_VERSION,
            )

        fired = [
            r for r in results.values()
            if r.verdict in (AXIS_VIOLATION, AXIS_ERROR)
        ]
        blocked = [r for r in fired if r.policy == POLICY_FAIL_CLOSED]
        if blocked:
            verdict = VERDICT_BLOCKED
        elif fired:
            verdict = VERDICT_DEGRADED
        else:
            verdict = VERDICT_AVAILABLE

        today = _session_date(ctx)
        block: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "date": today.isoformat(),
            "run_mode": getattr(ctx, "_run_mode", None),
            "verdict": verdict,
            "degraded": bool(fired),
            "blocked": bool(blocked),
            "axes": {name: r.as_dict() for name, r in results.items()},
            "fired": [
                {
                    "axis": r.axis,
                    "policy": r.policy,
                    "reason": "; ".join(r.violations) or r.error or r.verdict,
                    "evidence": dict(r.evidence),
                }
                for r in fired
            ],
            "axes_evaluated": [
                name for name, r in results.items() if r.verdict != AXIS_SKIP
            ],
            "missing_contracts": missing_contracts,
            "error": None,
        }
        return block, blocked

    def _run_checker(
        self, ctx: Any, name: str, contract: dict, *,
        declared_in_config: bool,
        checker: "Callable[[Any, dict], AxisResult]",
    ) -> AxisResult:
        policy = str(contract.get("policy", POLICY_DEGRADE))
        if policy not in _KNOWN_POLICIES:
            log.warning(
                "data_contracts.axes[%r].policy=%r unknown — treating as %s",
                name, policy, POLICY_DEGRADE,
            )
            policy = POLICY_DEGRADE
        if contract.get("enabled", True) is False:
            result = AxisResult(name, verdict=AXIS_SKIP)
            result.evidence["note"] = "axis disabled via contract"
        else:
            try:
                result = checker(ctx, contract)
            except Exception as exc:  # noqa: BLE001 — fail-isolated for degrade;
                # a fail_closed axis converts this error into a block below.
                log.exception(
                    "data-availability checker %r raised — axis marked error",
                    name,
                )
                result = AxisResult(name, error=f"{type(exc).__name__}: {exc}")
        result.policy = policy
        result.contract_declared = declared_in_config
        result.contract = dict(contract)
        return result.finalize()

    @staticmethod
    def _any_fail_closed_declared(cfg: dict) -> bool:
        axes = ((cfg.get("data_contracts") or {}).get("axes") or {})
        return any(
            isinstance(c, dict) and c.get("policy") == POLICY_FAIL_CLOSED
            for c in axes.values()
        )

    @staticmethod
    def _mirror_counters(ctx: Any, block: dict[str, Any]) -> None:
        counters = getattr(ctx, "counters", None)
        if not isinstance(counters, dict):
            return
        counters["data_availability_fired"] = len(block["fired"])
        counters["data_availability_degraded"] = int(bool(block["degraded"]))
        counters["data_availability_blocked"] = int(bool(block["blocked"]))

    @staticmethod
    def _record_error(ctx: Any, exc: Exception) -> None:
        log.exception(
            "DataAvailabilityGateTask failed — verify-only, run continues "
            "(no fail_closed axis declared)",
        )
        try:
            counters = getattr(ctx, "counters", None)
            if isinstance(counters, dict):
                counters["data_availability_errors"] = (
                    int(counters.get("data_availability_errors", 0)) + 1
                )
            if not isinstance(getattr(ctx, CTX_ATTR, None), dict):
                setattr(ctx, CTX_ATTR, {
                    "schema": SCHEMA_VERSION,
                    "date": _session_date(ctx).isoformat(),
                    "run_mode": getattr(ctx, "_run_mode", None),
                    "verdict": None,
                    "degraded": False,
                    "blocked": False,
                    "axes": {},
                    "fired": [],
                    "axes_evaluated": [],
                    "missing_contracts": [],
                    "error": f"{type(exc).__name__}: {exc}",
                })
        except Exception:  # noqa: BLE001 — even error handling must not raise
            log.exception("DataAvailabilityGateTask error-handler failed")
