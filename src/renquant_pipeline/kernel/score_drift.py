"""Score-distribution drift audit — PSI of rank_score vs a trailing baseline.

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §L6 audit
sidecar (catalog item 3) + the operator's "pipeline 中应该有自行审计 task …
early detect data abnormal" mandate. Graduates
scripts/engineering/score_drift_audit_prototype.py.

Population Stability Index on today's calibrated rank_score distribution
vs a trailing-N-run baseline. PSI bands are the industry standard:
  < 0.10  INFO     (stable)
  < 0.25  WARN     (moderate shift — investigate)
  >= 0.25 CRITICAL (population changed — calibrator collapse / feature
                    drift / scorer swap)

Pure core (psi / severity / score_drift_report) — no DB, no I/O — so it
unit-tests without fixtures; the DB-query helper is a thin separable
adapter. Read-only by construction: this module never writes a decision.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

INFO_BAND = 0.10
WARN_BAND = 0.25
MIN_SCORES_PER_RUN = 30   # a "full scoring run" floor; below = sell-only/partial


def psi(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index. Quantile bins from ``expected``; ±inf
    edges so out-of-range ``actual`` lands in the tail bins; 1e-6 floor so
    an empty bin never produces a div-by-zero or log(0)."""
    expected = np.asarray(expected, dtype=float)
    actual = np.asarray(actual, dtype=float)
    qs = np.quantile(expected, np.linspace(0, 1, bins + 1))
    qs[0], qs[-1] = -np.inf, np.inf
    e, _ = np.histogram(expected, qs)
    a, _ = np.histogram(actual, qs)
    e = np.clip(e / e.sum(), 1e-6, None)
    a = np.clip(a / a.sum(), 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


def severity(value: float) -> str:
    return ("INFO" if value < INFO_BAND
            else "WARN" if value < WARN_BAND
            else "CRITICAL")


#: Trials per size when estimating the null PSI. 200 is enough to pin a median
#: to ~2 decimals and costs single-digit milliseconds at these array sizes;
#: the estimate is reported, never used as a gate.
_NULL_TRIALS = 200

#: Cache keyed on (n_baseline, n_current, bins, trials, seed). The null floor
#: depends only on the SHAPE of the comparison, not on the values, so a day's
#: repeated audits at the same sizes/trials/seed pay for it once. `trials` and
#: `seed` are part of the key (not just the shape) because a caller raising
#: precision or varying the RNG for a robustness check must get a fresh
#: estimate, not a stale one from the first call at that shape.
_NULL_FLOOR_CACHE: dict[tuple[int, int, int, int, int], float] = {}


def null_psi_floor(n_baseline: int, n_current: int, bins: int = 10,
                   *, trials: int = _NULL_TRIALS, seed: int = 20260807) -> float:
    """Median PSI when `current` is drawn from the SAME law as `baseline`.

    WHY THIS IS REPORTED ALONGSIDE EVERY PSI (measured 2026-08-07). The bands
    below are the textbook cut-offs, and they assume the two samples are
    comparably sized. In production they are not: over 1,082 live audits,
    `n_baseline` ran ~1,500 while `n_current` was **under 100 every single
    time** (median 83) — a multi-day accumulated pool against one day's
    cross-section. At that shape the ZERO-DRIFT median PSI is already ~0.118,
    about 7x the ~0.016 it would be at matched sizes, because `psi()` floors an
    empty bin at 1e-6 and one empty bin alone contributes ~1.15 — 4.6x the
    whole CRITICAL threshold. Empty bins are common when 83 names fall into 10
    quantile bins.

    So `CRITICAL` (>=0.25) sits barely 2x above where a perfectly stable model
    lands, and 83% of live audits fire it. That is NOT proof the alarm is
    meaningless: a placebo at n=83 fires CRITICAL only 6% of the time, so
    sample size explains the raised floor, not the 83%. The live median 0.345
    is 2.9x the floor and the excess is real and undiagnosed.

    This function changes no verdict. It exists so a reader can see how far
    above the noise floor a value actually sits, which is the difference
    between "0.345, CRITICAL" and "0.345 against a 0.118 floor".
    """
    key = (int(n_baseline), int(n_current), int(bins), int(trials), int(seed))
    hit = _NULL_FLOOR_CACHE.get(key)
    if hit is not None:
        return hit
    if n_baseline < bins or n_current <= 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(int(n_baseline))
    vals = [psi(base, rng.standard_normal(int(n_current)), bins=bins)
            for _ in range(int(trials))]
    out = float(np.median(vals))
    _NULL_FLOOR_CACHE[key] = out
    return out


@dataclass(frozen=True)
class DriftReport:
    psi: float
    severity: str
    n_baseline: int
    n_current: int
    ok: bool          # True for INFO; WARN/CRITICAL are findings
    #: Median PSI under zero drift at THIS comparison's sizes. Reported, never
    #: gated on — see `null_psi_floor`.
    null_floor: float = float("nan")
    #: psi / null_floor. >1 means "above the noise this shape produces on its
    #: own"; ~1 means the value is what a stable model looks like here.
    excess_over_floor: float = float("nan")


def score_drift_report(baseline: np.ndarray, current: np.ndarray,
                       bins: int = 10) -> DriftReport:
    """PSI + banded verdict for two score arrays.

    Degenerate inputs (either side too small to bin) return a WARN
    finding rather than a number — "we could not measure stability" is a
    signal, not a pass (no-silent-continue)."""
    baseline = np.asarray(baseline, dtype=float)
    current = np.asarray(current, dtype=float)
    if baseline.size < bins or current.size == 0:
        return DriftReport(psi=float("nan"), severity="WARN",
                           n_baseline=int(baseline.size),
                           n_current=int(current.size), ok=False)
    v = psi(baseline, current, bins=bins)
    sev = severity(v)
    floor = null_psi_floor(baseline.size, current.size, bins=bins)
    excess = (v / floor) if (floor and np.isfinite(floor) and floor > 0) else float("nan")
    return DriftReport(psi=v, severity=sev, n_baseline=int(baseline.size),
                       n_current=int(current.size), ok=(sev == "INFO"),
                       null_floor=floor, excess_over_floor=float(excess))


def load_score_drift_from_db(conn, *, trailing: int = 20,
                             bins: int = 10) -> DriftReport | None:
    """Build a DriftReport from a runs DB's candidate_scores table:
    latest full scoring run vs the prior ``trailing`` full runs. Returns
    None when there are too few full runs to baseline. Read-only."""
    rows = conn.execute(
        "SELECT run_id, rank_score FROM candidate_scores "
        "WHERE rank_score IS NOT NULL").fetchall()
    by_run: dict[str, list[float]] = {}
    for run_id, score in rows:
        by_run.setdefault(str(run_id), []).append(float(score))
    full = sorted(rid for rid, vals in by_run.items()
                  if len(vals) >= MIN_SCORES_PER_RUN)  # run_id is date-prefixed
    if len(full) < 3:
        return None
    latest = full[-1]
    baseline_ids = full[-(trailing + 1):-1]
    baseline = np.array([s for rid in baseline_ids for s in by_run[rid]])
    current = np.array(by_run[latest])
    return score_drift_report(baseline, current, bins=bins)
