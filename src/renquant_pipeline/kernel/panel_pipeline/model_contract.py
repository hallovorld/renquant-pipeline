"""Panel model input/output contract validation utilities.

This module is the multirepo home for the soft guards used by runtime panel
scoring and calibration. It was lifted from the umbrella training-panel
contract so ``renquant-pipeline`` can score artifacts without importing the
RenQuant strategy checkout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


log = logging.getLogger(__name__)


class HeadInputError(ValueError):
    """Raised when input panel violates the feature contract."""


class HeadOutputError(ValueError):
    """Raised when prediction output violates the diversity contract."""


@dataclass
class CheckResult:
    ok: bool
    n_rows: int
    n_cols: int
    n_zero_var_cols: int
    n_nan_rows: int
    mu_xs_std: float | None = None
    sigma_xs_std: float | None = None
    n_unique_mu: int | None = None
    n_finite_mu: int | None = None
    warnings: list[str] | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []


INPUT_ZERO_VAR_FRAC_HARD = 0.50
INPUT_ZERO_VAR_FRAC_SOFT = 0.10
INPUT_NAN_FRAC_HARD = 0.50

OUTPUT_MU_XS_STD_HARD = 1e-6
OUTPUT_MU_XS_STD_SOFT = 1e-4
OUTPUT_MIN_UNIQUE_MU = 2
OUTPUT_FINITE_FRAC_HARD = 0.50

OUTPUT_SERIES_XS_STD_HARD = 1e-8
OUTPUT_SERIES_MIN_UNIQUE = 2


def soft_check_input(
    X: pd.DataFrame,
    feature_cols: Iterable[str],
    *,
    head_name: str = "head",
) -> CheckResult:
    """Inspect an input panel without raising."""
    feat_cols = list(feature_cols)
    present = [col for col in feat_cols if col in X.columns]
    n_rows = len(X)
    n_cols = len(present)
    if n_rows == 0 or n_cols == 0:
        return CheckResult(
            ok=False,
            n_rows=n_rows,
            n_cols=n_cols,
            n_zero_var_cols=0,
            n_nan_rows=0,
            warnings=["empty input"],
        )

    sub = X[present]
    n_nan_rows = int(sub.isna().all(axis=1).sum())
    if n_rows >= 2:
        col_stds = sub.std(axis=0, skipna=True).fillna(0.0).values
        n_zero_var_cols = int((np.abs(col_stds) < 1e-12).sum())
    else:
        n_zero_var_cols = 0

    pct_zero = n_zero_var_cols / max(1, n_cols)
    pct_nan_rows = n_nan_rows / max(1, n_rows)
    res = CheckResult(
        ok=True,
        n_rows=n_rows,
        n_cols=n_cols,
        n_zero_var_cols=n_zero_var_cols,
        n_nan_rows=n_nan_rows,
    )
    if pct_zero > INPUT_ZERO_VAR_FRAC_HARD or pct_nan_rows > INPUT_NAN_FRAC_HARD:
        res.ok = False
        res.warnings.append(
            f"{head_name}.input HARD FAIL: pct_zero_var_cols={pct_zero:.1%} "
            f"(>{INPUT_ZERO_VAR_FRAC_HARD:.0%}), pct_nan_rows={pct_nan_rows:.1%}"
        )
        log.error("[model_contract] %s", res.warnings[-1])
    elif pct_zero > INPUT_ZERO_VAR_FRAC_SOFT:
        res.warnings.append(
            f"{head_name}.input SOFT: pct_zero_var_cols={pct_zero:.1%} "
            f"(>{INPUT_ZERO_VAR_FRAC_SOFT:.0%} warn floor) - partial constants"
        )
        log.warning("[model_contract] %s", res.warnings[-1])
    return res


def validate_input_panel(
    X: pd.DataFrame,
    feature_cols: Iterable[str],
    *,
    head_name: str = "head",
) -> CheckResult:
    """Strict variant of ``soft_check_input``."""
    res = soft_check_input(X, feature_cols, head_name=head_name)
    if not res.ok:
        raise HeadInputError("; ".join(res.warnings))
    return res


def soft_check_output(
    out: pd.DataFrame,
    *,
    head_name: str = "head",
) -> CheckResult:
    """Inspect a distributional model output without raising."""
    if "mu" not in out.columns or "sigma" not in out.columns:
        res = CheckResult(
            ok=False,
            n_rows=len(out),
            n_cols=0,
            n_zero_var_cols=0,
            n_nan_rows=0,
            warnings=[f"{head_name}.output missing mu/sigma columns"],
        )
        log.error("[model_contract] %s", res.warnings[-1])
        return res

    mu = pd.to_numeric(out["mu"], errors="coerce").values
    sigma = pd.to_numeric(out["sigma"], errors="coerce").values
    finite = np.isfinite(mu)
    n_total = len(mu)
    n_finite = int(finite.sum())
    pct_finite = n_finite / max(1, n_total)
    if n_finite < 2:
        res = CheckResult(
            ok=False,
            n_rows=n_total,
            n_cols=0,
            n_zero_var_cols=0,
            n_nan_rows=int(n_total - n_finite),
            n_finite_mu=n_finite,
            warnings=[
                f"{head_name}.output HARD FAIL: only {n_finite}/{n_total} "
                "finite mu rows (need >=2 for diversity check)"
            ],
        )
        log.error("[model_contract] %s", res.warnings[-1])
        return res

    mu_f = mu[finite]
    sigma_f = sigma[finite]
    mu_xs_std = float(mu_f.std())
    sigma_xs_std = float(sigma_f.std()) if len(sigma_f) else 0.0
    n_unique_mu = int(len(np.unique(np.round(mu_f, 8))))
    res = CheckResult(
        ok=True,
        n_rows=n_total,
        n_cols=0,
        n_zero_var_cols=0,
        n_nan_rows=int(n_total - n_finite),
        mu_xs_std=mu_xs_std,
        sigma_xs_std=sigma_xs_std,
        n_unique_mu=n_unique_mu,
        n_finite_mu=n_finite,
    )
    if pct_finite < OUTPUT_FINITE_FRAC_HARD:
        res.ok = False
        res.warnings.append(
            f"{head_name}.output HARD FAIL: only {pct_finite:.1%} finite mu "
            f"(< {OUTPUT_FINITE_FRAC_HARD:.0%})"
        )
        log.error("[model_contract] %s", res.warnings[-1])
    if mu_xs_std < OUTPUT_MU_XS_STD_HARD or n_unique_mu < OUTPUT_MIN_UNIQUE_MU:
        res.ok = False
        res.warnings.append(
            f"{head_name}.output HARD FAIL: mu collapsed - "
            f"x-sec std={mu_xs_std:.2e} (< {OUTPUT_MU_XS_STD_HARD:.0e}), "
            f"n_unique={n_unique_mu} (need >={OUTPUT_MIN_UNIQUE_MU}). "
            "Symptom of constant input features or a degenerate model."
        )
        log.error("[model_contract] %s", res.warnings[-1])
    elif mu_xs_std < OUTPUT_MU_XS_STD_SOFT:
        res.warnings.append(
            f"{head_name}.output SOFT: mu x-sec std={mu_xs_std:.2e} "
            f"(< {OUTPUT_MU_XS_STD_SOFT:.0e} warn floor)"
        )
        log.warning("[model_contract] %s", res.warnings[-1])
    return res


def validate_output_dist(
    out: pd.DataFrame,
    *,
    head_name: str = "head",
) -> CheckResult:
    """Strict variant of ``soft_check_output``."""
    res = soft_check_output(out, head_name=head_name)
    if not res.ok:
        raise HeadOutputError("; ".join(res.warnings))
    return res


def soft_check_score_series(
    scores: pd.Series,
    *,
    model_name: str = "model",
    expected_min: float | None = None,
    expected_max: float | None = None,
) -> CheckResult:
    """Inspect a univariate model score output without raising."""
    if not isinstance(scores, pd.Series):
        try:
            scores = pd.Series(scores)
        except Exception:
            res = CheckResult(
                ok=False,
                n_rows=0,
                n_cols=1,
                n_zero_var_cols=0,
                n_nan_rows=0,
                warnings=[f"{model_name}.score: not a pd.Series"],
            )
            log.error("[model_contract] %s", res.warnings[-1])
            return res

    arr = pd.to_numeric(scores, errors="coerce").values
    finite = np.isfinite(arr)
    n_total = len(arr)
    n_finite = int(finite.sum())
    res = CheckResult(
        ok=True,
        n_rows=n_total,
        n_cols=1,
        n_zero_var_cols=0,
        n_nan_rows=int(n_total - n_finite),
    )
    if n_finite < 2:
        res.ok = False
        res.warnings.append(
            f"{model_name}.score HARD FAIL: only {n_finite}/{n_total} finite "
            "values (need >=2 for diversity)"
        )
        log.error("[model_contract] %s", res.warnings[-1])
        return res

    fa = arr[finite]
    xs_std = float(fa.std())
    n_unique = int(len(np.unique(np.round(fa, 10))))
    res.mu_xs_std = xs_std
    res.n_unique_mu = n_unique
    res.n_finite_mu = n_finite
    if xs_std < OUTPUT_SERIES_XS_STD_HARD or n_unique < OUTPUT_SERIES_MIN_UNIQUE:
        res.ok = False
        res.warnings.append(
            f"{model_name}.score HARD FAIL: collapsed prediction - "
            f"x-sec std={xs_std:.2e}, n_unique={n_unique}. Symptom of "
            "constant input or degenerate model."
        )
        log.error("[model_contract] %s", res.warnings[-1])
    if expected_min is not None and float(fa.min()) < expected_min:
        res.ok = False
        res.warnings.append(
            f"{model_name}.score HARD FAIL: min={fa.min():.4f} < {expected_min}"
        )
        log.error("[model_contract] %s", res.warnings[-1])
    if expected_max is not None and float(fa.max()) > expected_max:
        res.ok = False
        res.warnings.append(
            f"{model_name}.score HARD FAIL: max={fa.max():.4f} > {expected_max}"
        )
        log.error("[model_contract] %s", res.warnings[-1])
    if (n_total - n_finite) / max(1, n_total) > 0.25:
        res.warnings.append(
            f"{model_name}.score SOFT: {n_total - n_finite}/{n_total} non-finite (>25%)"
        )
        log.warning("[model_contract] %s", res.warnings[-1])
    return res


def validate_score_series(
    scores: pd.Series,
    *,
    model_name: str = "model",
    expected_min: float | None = None,
    expected_max: float | None = None,
) -> CheckResult:
    """Strict variant of ``soft_check_score_series``."""
    res = soft_check_score_series(
        scores,
        model_name=model_name,
        expected_min=expected_min,
        expected_max=expected_max,
    )
    if not res.ok:
        raise HeadOutputError("; ".join(res.warnings))
    return res
