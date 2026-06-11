"""DataVerificationTask — verify the auxiliary feature feeds in the daily run.

``DataFreshnessGateTask`` covers OHLCV only. The 2026-06-11 R2 audit found
``sec_fundamentals_daily.parquet`` frozen at 2026-02-10 with NO pipeline check —
its 5 fundamental features were a stale constant fed into every live bar while
the price block stayed fresh, a train/serve drift that worsened daily.

This task makes data verification a first-class daily-pipeline stage: it checks
the extra-feature feeds (fundamentals, earnings/PEAD/SUE, sentiment) for
staleness and watchlist coverage, stamps a structured report onto
``ctx._data_verification`` + ``ctx.counters``, warns loudly per source, and
optionally fails closed (``data_verification.hard_fail = true``). Default warns
(does not break runs); the operator opts into blocking.

Config (``config.data_verification``):
  enabled (default True), hard_fail (default False),
  sources.<name>.{max_stale_days, min_coverage, check_staleness}
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline._data_root import data_root

from .pipeline import Task

log = logging.getLogger("kernel.pipeline.data_verification")

# Per-source defaults. Fundamentals/sentiment are daily forward-filled feeds and
# must stay current; earnings are quarterly (coverage only, no staleness).
_SOURCE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fundamentals": {
        "kind": "single_parquet", "rel": "data/sec_fundamentals_daily.parquet",
        "check_staleness": True, "max_stale_days": 20, "min_coverage": 0.80,
    },
    "sentiment": {
        "kind": "per_ticker_dir", "rel": "data/news_sentiment_alpaca",
        "check_staleness": True, "max_stale_days": 7, "min_coverage": 0.40,
    },
    "earnings": {
        "kind": "per_ticker_dir", "rel": "data/earnings_surprise",
        "check_staleness": False, "min_coverage": 0.70,
    },
}


def _blank_report(n_expected: int) -> dict[str, Any]:
    return {"present": False, "max_date": None, "stale_days": None,
            "coverage": None, "n_have": 0, "n_expected": n_expected,
            "ok": False, "reasons": []}


def _source_ok(rep: dict[str, Any], spec: dict[str, Any]) -> bool:
    if not rep["present"]:
        return False
    ok = True
    if spec.get("check_staleness") and rep["stale_days"] is not None:
        max_stale = spec.get("max_stale_days")
        if max_stale is not None and rep["stale_days"] > int(max_stale):
            rep["reasons"].append(
                f"stale {rep['stale_days']}d > {max_stale}d "
                f"(latest {rep['max_date']})")
            ok = False
    if rep["coverage"] is not None:
        min_cov = float(spec.get("min_coverage", 0.0) or 0.0)
        if rep["coverage"] < min_cov:
            rep["reasons"].append(
                f"coverage {rep['coverage']:.0%} < {min_cov:.0%} "
                f"({rep['n_have']}/{rep['n_expected']})")
            ok = False
    return ok


def _verify_single_parquet(root: Path, spec: dict[str, Any],
                           wl: set[str], today_ts: pd.Timestamp | None) -> dict[str, Any]:
    rep = _blank_report(len(wl))
    path = root / spec["rel"]
    if not path.exists():
        rep["reasons"].append(f"missing {path}")
        return rep
    rep["present"] = True
    try:
        try:
            df = pd.read_parquet(path, columns=["date", "ticker"])
        except Exception:
            df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        rep["reasons"].append(f"read error: {exc}")
        return rep
    if spec.get("check_staleness") and "date" in df.columns:
        dates = pd.to_datetime(df["date"], errors="coerce")
        md = dates.max()
        if pd.notna(md):
            rep["max_date"] = md.date().isoformat()
            if today_ts is not None:
                rep["stale_days"] = int((today_ts - md).days)
    if wl and "ticker" in df.columns:
        have = {str(t) for t in df["ticker"].unique()} & wl
        rep["n_have"] = len(have)
        rep["coverage"] = len(have) / len(wl)
    rep["ok"] = _source_ok(rep, spec)
    return rep


def _verify_per_ticker_dir(root: Path, spec: dict[str, Any],
                           wl: set[str], today_ts: pd.Timestamp | None) -> dict[str, Any]:
    rep = _blank_report(len(wl))
    d = root / spec["rel"]
    if not d.is_dir():
        rep["reasons"].append(f"missing dir {d}")
        return rep
    rep["present"] = True
    files = {p.stem for p in d.glob("*.parquet")}
    have = (files & wl) if wl else files
    rep["n_have"] = len(have)
    if wl:
        rep["coverage"] = len(have) / len(wl)
    if spec.get("check_staleness"):
        max_d = None
        for t in list(have)[:8]:  # sample for a freshness signal
            try:
                sdf = pd.read_parquet(d / f"{t}.parquet", columns=["date"])
                m = pd.to_datetime(sdf["date"], errors="coerce").max()
                if pd.notna(m) and (max_d is None or m > max_d):
                    max_d = m
            except Exception:  # noqa: BLE001
                continue
        if max_d is not None:
            rep["max_date"] = max_d.date().isoformat()
            if today_ts is not None:
                rep["stale_days"] = int((today_ts - max_d).days)
    rep["ok"] = _source_ok(rep, spec)
    return rep


def verify_feature_data_sources(
    root: Path, watchlist: Any, today: Any, cfg: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Pure check: return a per-source verification report."""
    wl = {str(t) for t in (watchlist or [])}
    today_ts = pd.Timestamp(today) if today is not None else None
    overrides = (cfg.get("sources") or {}) if isinstance(cfg, dict) else {}
    report: dict[str, dict[str, Any]] = {}
    for name, default in _SOURCE_DEFAULTS.items():
        spec = {**default, **(overrides.get(name) or {})}
        if spec.get("kind") == "single_parquet":
            report[name] = _verify_single_parquet(root, spec, wl, today_ts)
        else:
            report[name] = _verify_per_ticker_dir(root, spec, wl, today_ts)
    return report


class DataVerificationTask(Task):
    """Verify auxiliary feature feeds (fundamentals/earnings/sentiment).

    Stamps ``ctx._data_verification`` and ``ctx.counters
    ['data_verification_failures']``. Warns per failing source; raises only
    when ``data_verification.hard_fail`` is set. Disabled via
    ``data_verification.enabled = false`` (e.g. backtests / known-stale envs).
    """

    def run(self, ctx) -> bool:
        cfg = (getattr(ctx, "config", None) or {}).get("data_verification", {}) or {}
        if cfg.get("enabled", True) is False:
            log.info("DataVerificationTask: disabled via config — skipping")
            return True
        try:
            root = Path(data_root())
        except Exception as exc:  # noqa: BLE001
            log.warning("DataVerificationTask: could not resolve data root (%s) "
                        "— skipping feed verification", exc)
            return True

        watchlist = (getattr(ctx, "config", None) or {}).get("watchlist", [])
        report = verify_feature_data_sources(
            root, watchlist, getattr(ctx, "today", None), cfg)
        ctx._data_verification = report  # noqa: SLF001

        failing = [n for n, r in report.items() if not r["ok"]]
        for name, r in report.items():
            if r["ok"]:
                log.info(
                    "DataVerification[%s] ok (latest=%s, coverage=%s)",
                    name, r["max_date"],
                    f"{r['coverage']:.0%}" if r["coverage"] is not None else "n/a",
                )
            else:
                log.warning("DataVerification[%s] FAILED: %s", name,
                            "; ".join(r["reasons"]) or "unavailable")

        counters = getattr(ctx, "counters", None)
        if isinstance(counters, dict):
            counters["data_verification_failures"] = len(failing)

        if failing and bool(cfg.get("hard_fail", False)):
            raise RuntimeError(
                "DataVerificationTask: feed(s) failed verification and "
                f"hard_fail is on: {failing}. See ctx._data_verification.")
        return True
