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

import hashlib
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

#: Cache keyed on the BASELINE's exact content (a content digest) plus
#: (n_current, bins, trials, seed). The null is conditional on the baseline's
#: actual distribution and on what `psi()` reads from it at the requested
#: `bins` — size alone was NOT a sufficient key (codex on #279 review 1), and
#: neither was a fixed 21-point quantile grid (codex on #279 review 5): two
#: distinct baselines can share that coarse grid while producing different
#: bin edges/counts once `psi()` bins them — with ties straddling a quantile
#: boundary, or simply because `bins > 20` reads finer edges the 21-point grid
#: never captured — so the second lookup would silently reuse the first
#: baseline's floor. `trials` and `seed` are in the key because a caller
#: raising precision or varying the RNG for a robustness check must get a
#: fresh estimate, not a stale one.
_NULL_FLOOR_CACHE: dict[tuple, float] = {}


def _baseline_key(baseline: np.ndarray) -> bytes:
    """Exact identity for a baseline's content.

    A coarse summary (size + a fixed quantile grid) is not a sufficient key
    for what `psi()` actually reads from the baseline at an arbitrary `bins`
    — see the `_NULL_FLOOR_CACHE` note. Hashing the content is exact by
    construction regardless of `bins` or how ties are distributed, and at
    these array sizes (single-digit thousands of floats) the copy this
    forces is negligible next to the `trials` resampling loop it's guarding.
    """
    return hashlib.sha256(np.ascontiguousarray(baseline, dtype=np.float64).tobytes()).digest()


def null_psi_floor(baseline: np.ndarray, n_current: int, bins: int = 10,
                   *, trials: int = _NULL_TRIALS, seed: int = 20260807) -> float:
    """Median PSI when `current` is RESAMPLED FROM THE BASELINE ITSELF.

    WHY THIS IS REPORTED ALONGSIDE EVERY PSI (measured 2026-08-07). The bands
    below are the textbook cut-offs and assume comparably sized samples. In
    production they are not: over 1,082 live audits `n_baseline` ran ~1,500
    while `n_current` was **under 100 every single time** (median 83). At that
    shape the zero-drift median PSI is already far above zero, because `psi()`
    floors an empty bin at 1e-6 and one empty bin alone contributes ~1.15 —
    4.6x the whole CRITICAL threshold. Empty bins are common when 83 names fall
    into 10 quantile bins.

    WHY THE BASELINE ARRAY AND NOT JUST ITS SIZE (codex on #279). A first cut
    estimated the null from Gaussian draws keyed on
    `(n_baseline, n_current, bins)`, i.e. shape only. `psi()` is NOT shape-only:
    it builds bin edges from `np.quantile(expected, ...)`, so a baseline with
    ties collapses those edges and changes the effective bin count. Repro:
    with `base = np.repeat(np.arange(5), 300)` the shape-only estimate is
    0.1189 while resampling from that baseline gives 0.0370 — **3.2x
    overstated**. Resampling with replacement from the real baseline conditions
    the null on the distribution actually in play, ties and all, and removes a
    Gaussian assumption I had flagged as unverified in my own write-up and then
    not verified.

    Reported, never gated on: `severity` remains `severity(psi)`.
    """
    baseline = np.asarray(baseline, dtype=float)
    n_current = int(n_current)
    if baseline.size < bins or n_current <= 0:
        return float("nan")
    key = (_baseline_key(baseline), n_current, int(bins), int(trials), int(seed))
    hit = _NULL_FLOOR_CACHE.get(key)
    if hit is not None:
        return hit
    rng = np.random.default_rng(seed)
    vals = [psi(baseline, rng.choice(baseline, size=n_current, replace=True), bins=bins)
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
    #: Median PSI under zero drift, resampled from THIS comparison's actual
    #: baseline. Reported, never gated on — see `null_psi_floor`.
    null_floor: float = float("nan")
    #: psi / null_floor. >1 means "above the noise this shape produces on its
    #: own"; ~1 means the value is what a stable model looks like here.
    excess_over_floor: float = float("nan")
    #: The exact run_ids that made up `baseline`, in trailing-window order.
    #: Persisted alongside the row (PR #280 review, P1) so a later re-banding
    #: tool can prove it reconstructed the SAME baseline rather than a
    #: same-sized substitute after `candidate_scores` pruning — see
    #: scripts/audit_score_drift_excess.py. Empty when the caller didn't
    #: supply it (e.g. a report built directly from arrays in a test).
    baseline_run_ids: tuple[str, ...] = ()


def score_drift_report(baseline: np.ndarray, current: np.ndarray,
                       bins: int = 10, *,
                       baseline_run_ids: tuple[str, ...] = ()) -> DriftReport:
    """PSI + banded verdict for two score arrays.

    Degenerate inputs (either side too small to bin) return a WARN
    finding rather than a number — "we could not measure stability" is a
    signal, not a pass (no-silent-continue)."""
    baseline = np.asarray(baseline, dtype=float)
    current = np.asarray(current, dtype=float)
    if baseline.size < bins or current.size == 0:
        return DriftReport(psi=float("nan"), severity="WARN",
                           n_baseline=int(baseline.size),
                           n_current=int(current.size), ok=False,
                           baseline_run_ids=baseline_run_ids)
    v = psi(baseline, current, bins=bins)
    sev = severity(v)
    floor = null_psi_floor(baseline, current.size, bins=bins)
    excess = (v / floor) if (floor and np.isfinite(floor) and floor > 0) else float("nan")
    return DriftReport(psi=v, severity=sev, n_baseline=int(baseline.size),
                       n_current=int(current.size), ok=(sev == "INFO"),
                       null_floor=floor, excess_over_floor=float(excess),
                       baseline_run_ids=baseline_run_ids)


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
    return score_drift_report(baseline, current, bins=bins,
                              baseline_run_ids=tuple(baseline_ids))
