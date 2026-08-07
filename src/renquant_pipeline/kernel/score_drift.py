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
    #: The run_id of the CURRENT run this report measured (i.e. `latest` in
    #: `load_score_drift_from_db`) — NOT a member of `baseline_run_ids`.
    #: Lets a persist-only caller (scripts/score_drift_monitor.py) record
    #: which run an audit row is about instead of writing `run_id=None`,
    #: which made every monitor-persisted row permanently unscorable by
    #: audit_score_drift_excess.py's `run_id IS NOT NULL` check (PR #280
    #: review, P1 round 3). None when the caller didn't supply it.
    run_id: str | None = None


def score_drift_report(baseline: np.ndarray, current: np.ndarray,
                       bins: int = 10, *,
                       baseline_run_ids: tuple[str, ...] = (),
                       run_id: str | None = None) -> DriftReport:
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
                           baseline_run_ids=baseline_run_ids, run_id=run_id)
    v = psi(baseline, current, bins=bins)
    sev = severity(v)
    floor = null_psi_floor(baseline, current.size, bins=bins)
    excess = (v / floor) if (floor and np.isfinite(floor) and floor > 0) else float("nan")
    return DriftReport(psi=v, severity=sev, n_baseline=int(baseline.size),
                       n_current=int(current.size), ok=(sev == "INFO"),
                       null_floor=floor, excess_over_floor=float(excess),
                       baseline_run_ids=baseline_run_ids, run_id=run_id)


def load_candidate_scores_by_run(conn) -> dict[str, list[float]]:
    """run_id -> rank_score list, CANDIDATE ROWS ONLY, for every run still
    holding raw scores in a runs DB's ``candidate_scores`` table.

    CANDIDATE ROWS ONLY (orch#899). `candidate_scores` holds two populations
    whose `rank_score` is not the same quantity, measured 2026-08-07 on
    `data/runs.alpaca.db`: within a single run, candidates span
    ``[-2.667, 3.050]`` (the z-composite the scorer emits) while holdings span
    ``[0.104, 0.340]`` — `ApplyGlobalCalibrationTask` writes the hold side as
    ``cal.calibrate_probability(panel_score)``, a bounded probability, on every
    run. Pooling them makes this a PSI over a mixture of two incommensurable
    units, which is not a distribution statistic about anything.

    `role IS NULL` is admitted for pre-role-column rows: the column was added
    later, and excluding legacy rows would silently shorten historical baselines
    rather than leaving them as they were.

    The `role` COLUMN itself is optional. Older runs DBs — and every minimal
    test fixture — create `candidate_scores(run_id, rank_score)` with no `role`
    at all, and an unconditional filter turns those into
    ``OperationalError: no such column: role``. The column is probed rather than
    assumed, and its absence degrades to the pre-fix pooled behaviour instead of
    crashing a monitor.

    Shared by every consumer of the drift population (orch#899 review, P1):
    `load_score_drift_from_db` below and `scripts/audit_score_drift_excess.py`
    both call this so a persisted ``n_baseline``/``baseline_run_ids`` and the
    read-only audit's reconstruction always agree on which rows count.
    """
    has_role = any(
        str(r[1]) == "role"
        for r in conn.execute("PRAGMA table_info(candidate_scores)").fetchall()
    )
    role_clause = " AND (role IS NULL OR role = 'candidate')" if has_role else ""
    rows = conn.execute(
        "SELECT run_id, rank_score FROM candidate_scores "
        "WHERE rank_score IS NOT NULL" + role_clause).fetchall()
    by_run: dict[str, list[float]] = {}
    for run_id, score in rows:
        by_run.setdefault(str(run_id), []).append(float(score))
    return by_run


def load_score_drift_from_db(conn, *, trailing: int = 20,
                             bins: int = 10) -> DriftReport | None:
    """Build a DriftReport from a runs DB's candidate_scores table:
    latest full scoring run vs the prior ``trailing`` full runs. Returns
    None when there are too few full runs to baseline. Read-only.

    Candidate rows only — see `load_candidate_scores_by_run`.

    This does NOT quiet the alarm, and that is not what it is for: on the live
    DB the same window moves 3.5956 -> 4.6600 once holdings are dropped, because
    the probability-scale rows were diluting the z-scale ones rather than
    inflating them.

    It also drops ONE run from the 95 that previously qualified as "full": a run
    that cleared `MIN_SCORES_PER_RUN` only because holding rows padded it past
    30. Counting a sell-only bar as a full scoring run because positions were
    persisted alongside it is the same error in a different place.
    """
    by_run = load_candidate_scores_by_run(conn)
    full = sorted(rid for rid, vals in by_run.items()
                  if len(vals) >= MIN_SCORES_PER_RUN)  # run_id is date-prefixed
    if len(full) < 3:
        return None
    latest = full[-1]
    baseline_ids = full[-(trailing + 1):-1]
    baseline = np.array([s for rid in baseline_ids for s in by_run[rid]])
    current = np.array(by_run[latest])
    return score_drift_report(baseline, current, bins=bins,
                              baseline_run_ids=tuple(baseline_ids),
                              run_id=latest)
