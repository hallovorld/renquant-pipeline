# Deep Bug Audit — Panel SCORING + CALIBRATOR layer

Date: 2026-06-10
Auditor scope: `src/renquant_pipeline/kernel/panel_pipeline/` — panel scorer, score
computation, global calibration (`ApplyGlobalCalibrationTask` /
`global_calibrator.py`), the calibrator that maps `panel_score → expected_return (mu)`,
`job_panel_scoring.py` scorer dispatch, hf_patchtst + xgb scorers, sigma/NGBoost
application.

Method: read-only static analysis + reproduction with
`/Users/renhao/git/github/RenQuant/.venv/bin/python` against live calibrator
artifacts and `RenQuant/data/runs.alpaca.db` (`candidate_scores`,
`score_distribution`). Branch read: `renquant-pipeline@main` (HEAD 221a328,
incl. PatchTST frozen-window fix PR #82 and signal-direction BUY gate PR #81).

KNOWN bugs (PatchTST frozen window, calibrator-positive-mu-on-bearish-then-gated)
are NOT re-reported as new; finding F1 deepens the calibrator question as the
prompt requested.

Severity legend: **blocker** = mis-sized/wrong-direction live trades or systemic
mis-scoring; **high** = wrong economic value reaching Kelly/QP under a plausible
config; **med** = latent / config-gated correctness bug; **low** = robustness /
dead-path / fragility.

---

## Summary of findings by severity

| ID | Severity | One-line |
|----|----------|----------|
| F1 | blocker  | PatchTST `panel_score` sign is NOT directional; calibrator maps bearish (negative) raw scores to positive μ and >0.5 probability — live on 2026-06-09/10. |
| F2 | blocker  | HF PatchTST CSRankNorm is applied over the 3-ticker candidate subset at inference vs ~142-ticker universe at training — out-of-distribution inputs, systematically biased scores. |
| F3 | high     | CHOPPY (and other) regime calibrators have an entirely-positive `er_y`: every panel_score, including the most bearish, maps to positive expected return. |
| F4 | high     | Kelly mixes horizons: μ on the 60d calibrator horizon, σ on a 252d annualized realized-vol fallback — `f*=μ_60d/σ²_252d` unless operator sets `sigma_horizon_days`. |
| F5 | high     | Calibrator extrapolation is a hard CLAMP to endpoints; live scores routinely exceed the fit ceiling so strong signals saturate to one μ/prob (rank information lost). |
| F6 | med      | `GlobalPanelCalibration.__post_init__` validates monotonicity of `x` only, never of `y` — a non-monotone/sign-flipping `er_y`/`prob_y` is accepted silently. |
| F7 | med      | Regime calibrators carry `lookahead_days=10` but the pooled is 60d; ER is linearly scaled ×6 to the 60d rotation horizon, mixing native horizons inconsistently. |
| F8 | med      | `sigma_horizon_days: null` in config → `float(None)` → NaN → every candidate Kelly-zeroed with `sigma_horizon_invalid`. Silent total-sizing-loss fragility. |
| F9 | low      | HF PatchTST distributional-head σ (`_last_sigma`) is computed and stored but never read; the model's own uncertainty is discarded. |
| F10| low      | `ApplyGlobalCalibrationTask` runs `soft_check_score_series` but ignores its `ok` result; a hard-fail (collapsed/out-of-range) only logs, never fails closed. |

Counts: blocker 2, high 3, med 3, low 2 (10 substantiated). Other items
explicitly checked and found NOT bugs are listed at the end so the next auditor
does not re-walk them.

---

## F1 — [BLOCKER] PatchTST panel_score sign is not directional; calibrator turns bearish raw scores into positive μ and BUY-grade probability

Files:
- `global_calibrator.py:66-79` (`expected_return`, `np.interp` with endpoint clamp)
- `global_calibrator.py:46-58` (`calibrate_probability`)
- `job_panel_scoring.py:2247-2273` (`ApplyGlobalCalibrationTask.run` writes `rank_score`, `expected_return`, `mu`)

Mechanism: The HF PatchTST score distribution is centered well below zero
(see F2), and the calibrator was fit on that same negative distribution. The
live calibrator
`artifacts/shadow/panel-rank-calibration.hf_patchtst_seed44_trainfit_20230103_20240409.json`
has `er_x ∈ [-0.3501, +0.0827]` and `prob_y ∈ [0.307, 0.802]`. Its "neutral"
(P=0.5) crossing sits around raw `≈ -0.13`, NOT at raw 0. Consequence: a
*negative* raw panel_score is near the TOP of the fit domain and maps to a high
probability and positive μ. The sign of `panel_score` is therefore not a
direction indicator — only cross-sectional rank is.

Reproduction (live calibrator):
```
prob at score=-0.04 (live ORCL) = 0.6834
mu   at score=-0.04             = +0.07325   (60d)
prob at score= 0.00 (neutral)   = 0.73
```

Live evidence (`runs.alpaca.db`):
- `score_distribution`, 2026-06-09 and 2026-06-10, held positions: `ORCL
  raw_panel=-0.0424 → rank_score=0.6807 mu=+0.0722`, `MU raw_panel=-0.1089 →
  rank_score=0.6031 mu=+0.0427`, `EQIX raw_panel=-0.1636 → rank_score=0.5347
  mu=+0.0167`. All BULL_CALM, all currently held on a bearish raw signal.
- 935 live `score_distribution` rows with `raw_panel<0 AND mu>0`.
- `candidate_scores`: of the 555 PatchTST-tagged candidate rows (every one of
  which has `panel_score<0`), 274 received `expected_return>0` and 147
  received `mu>0`.
- Repo-wide: 1233 `candidate_scores` rows `panel_score<0 AND expected_return>0`;
  25,891 `panel_score<0 AND mu>0`; 505 of the latter were `selected=1`.

Why this is deeper than the known gate: the signal-direction BUY gate (PR #81)
blocks a BUY when raw is bearish, but the *calibrator itself* is the thing
emitting positive μ / >0.5 probability for held names. Rotation/QP and
hold-side Kelly still consume `expected_return`/`mu`/`rank_score`
(`ApplyGlobalCalibrationTask` writes them on holdings at
`job_panel_scoring.py:2276-2296`), so the wrong-sign economic value reaches the
sizer for existing positions even with the buy gate on.

Is it intentional? Partially. An isotonic/Platt calibrator on a rank signal is
legitimately allowed to map a negative-but-high-rank score to positive μ. The
real defect is that nothing downstream knows that raw `panel_score=0` is not the
neutral point — there is no stored "neutral raw score" / base-rate-zero anchor,
so the buy gate's "raw<0 ⇒ bearish" assumption and the calibrator's "raw≈-0.13 ⇒
neutral" reality disagree.

Fix direction: persist the calibrator's neutral crossing (the raw score where
`prob=prob_base_rate` and `ER=0`) into metadata and make the
signal-direction/μ-sign logic test against THAT anchor, not against literal
`raw < 0`. Alternatively center the PatchTST score head (see F2) so raw 0 is the
cross-sectional median.

---

## F2 — [BLOCKER] HF PatchTST CSRankNorm computed over candidate subset, not the training universe

Files:
- `hf_patchtst_scorer.py:33-39` (`_csrank_norm_per_day`: `groupby("date").rank(pct=True)-0.5`)
- `hf_patchtst_scorer.py:283-299` (`score_with_history` applies it to whatever panel it is given)
- `job_panel_scoring.py:444-591` (`_build_live_panel_history`: alpha158 frames built ONLY for `target_tickers`)
- Training: `RenQuant/scripts/patchtst_hf.py:244-248,298,339` (CSRankNorm over the full ~291-ticker parquet panel)

Mechanism: At training, `csrank_norm_per_day` ranks each feature per-day across
the **entire dataset universe** (`pd.read_parquet(dataset_path)` → ~142–291
tickers/day). At inference the live panel is assembled in
`_build_live_panel_history` from `target_tickers` only (the candidate + holding
set), and `score_with_history` re-ranks per-day across just those rows. Live
PatchTST runs in `runs.alpaca.db` carry **3 tickers per run**
(`select run_id, count(*) ... where panel_ltr_artifact like '%patchtst%' group by
run_id` → 3,3,3,...). `rank(pct=True)-0.5` over 3 names can only take a handful
of discrete values; the model — trained on a smooth `[-0.5,+0.5]` spread over
~142 names — sees a wholly out-of-distribution input lattice.

Evidence of the resulting bias: EVERY PatchTST-tagged `panel_score` in the DB is
negative (`min=-0.2991 max=-0.0424 avg=-0.1946`, n=261 candidate rows /
555 incl. holdings). A correctly-normalized cross-section centered by
`-0.5` rank would straddle zero; a uniformly-negative output is the fingerprint
of subset-rank contamination (and it is what makes F1 fire).

This is the same *class* of defect as the frozen-window fix (PR #82): #82 fixed
the **time** axis of the sequence; the **cross-section** axis used by CSRankNorm
is still contaminated because the rank context is the post-gate candidate
subset, not the trained universe.

Note the XGB path explicitly guards against exactly this for its extra-feature
medians/ranks via `_stable_feature_context_tickers`
(`job_panel_scoring.py:114-164`) — that guard is NOT applied to the PatchTST
CSRankNorm.

Fix direction: build the PatchTST live panel (and run CSRankNorm) over the
stable training/watchlist cross-section (reuse `_stable_feature_context_tickers`
or the artifact's `feature_context_tickers`), then slice out scores for the
target tickers — so each ticker's rank is computed against the same universe the
model trained on.

---

## F3 — [HIGH] Regime calibrators with all-positive `er_y` map every score (incl. most bearish) to positive expected return

Files: `global_calibrator.py:66-90` (`expected_return` / `expected_return_vec`)
Artifacts: `artifacts/prod/panel-calibration-CHOPPY.json` (and to a lesser extent BULL_CALM/BEAR).

Mechanism: `expected_return` is a plain `np.interp` over `(er_x, er_y)`. There is
no constraint that `er_y` straddle zero. The CHOPPY calibrator's `er_y` is
entirely positive:
```
CHOPPY:    er_x [-0.0804,+0.0965]  er_y [+0.00707,+0.03051]  fraction er_y>0 = 1.000
```
So the most bearish in-domain score (er_x floor) returns μ=+0.0071, and a score
below the floor clamps to the same +0.0071 (reproduced: `score=-3.0 → mu=+0.00707`).
Under CHOPPY, μ is positive for the entire score axis — a structurally
long-only μ surface regardless of model conviction.

Reproduction:
```
panel_score=-3.000 -> mu=+0.00700   panel_score=0.000 -> mu=+0.01800   (CHOPPY-shaped head)
```

Not currently in the live config (`calibrator_per_regime: []`,
`regime_conditional.enabled: false`) but loadable via
`LoadGlobalCalibrationTask` whenever those knobs flip — and the pooled hf
calibrator (F1) already has 65% of `er_y` positive, so the effect is live in
weaker form.

Fix direction: at calibrator fit/load time, assert `er_y` crosses zero within
the fit domain (or carry an explicit "this regime has positive unconditional
drift" flag) and gate μ on the model's cross-sectional rank rather than on the
sign of the interpolated ER.

---

## F4 — [HIGH] Kelly numerator and denominator are on different horizons (60d μ vs 252d σ)

Files:
- μ scaled to `qp_mu_horizon` (default 60d): `job_panel_scoring.py:2229,2260-2272` + `_qp_mu_horizon_days:1963-1971`
- σ from realized-vol fallback is **annualized** (×√252): `_realized_vol_annualized:3031-3052`
- Kelly rescale only triggers when `sigma_horizon_days != 252`: `_rescale_annualized_sigma_for_kelly:3066-3069`, `ApplyKellySizingTask:3104,3157`

Mechanism: With the live prod stack (`ngboost.enabled=false`,
`use_calibrator_mu=true`, `use_realized_vol_fallback=true`), μ is the calibrator
ER scaled to the 60-day QP horizon while σ is annualized 60-day realized vol
(×√252). `kelly_target_pct` computes `f*=μ/σ²`. Unless the operator explicitly
sets `ranking.kelly_sizing.sigma_horizon_days=60`, σ is left at the 252-day unit
(`_kelly_sigma_horizon_days` default 252 → no rescale), so Kelly divides a
60-day drift by an annual variance — understating `f*` by ~6× (variance scales
~linearly in time: σ²_252/σ²_60 ≈ 252/60). The fix knob exists but the default
is the mismatched unit, and the code comments concede it
("Default 252 keeps that legacy unit; opt-in 60 aligns σ with the 60d
calibrator μ horizon").

Fix direction: derive `sigma_horizon_days` from the active μ horizon
(`qp_mu_horizon`) by default instead of a hardcoded 252; or compute realized vol
directly at the μ horizon rather than annualizing then rescaling.

---

## F5 — [HIGH] Calibrator extrapolation is a hard clamp; live scores exceed the fit domain so strong signals saturate to a single μ/prob

Files: `global_calibrator.py:50-58,70-79,90` (`np.interp(..., left=y[0], right=y[-1])`).

Mechanism: Outside `[x[0], x[-1]]` the heads do NOT extrapolate — they clamp to
the endpoint y. The prompt's "extrapolation" concern is really a **saturation /
clamp** concern: any score above the fit ceiling collapses to the SAME
`(er_y[-1], prob_y[-1])`, destroying rank information for the strongest signals.

Evidence: across all `candidate_scores`, panel_score spans `[-0.4441, +0.9050]`.
The live hf calibrator's `er_x` ceiling is only `+0.0827`; 3,802 / 206,447 rows
(1.8%) sit above it and 177 below the floor — those rows all share one clamped
μ/prob. For XGB pooled (`panel-rank-calibration.json`, er_x ceiling +0.562) the
overshoot is smaller, but the `recent-12mo` and `pre-2026-05-15-clip` artifacts
have `er_y` up to `+1.0` (a +100% ER knot), which the load-time clip
(`global_calibrator.py:167-174`) only catches at the ±0.20 bound.

Fix direction: either (a) refuse to score / fail-closed when a candidate's raw
score is materially outside the fit domain, or (b) keep clamp but emit a
per-bar counter of clamped candidates so saturation is observable (the
saturation guard at `2323-2395` checks output IQR, not input out-of-domain
fraction).

---

## F6 — [MED] Calibrator validates monotonicity of x only, never y — non-monotone / sign-flipping heads accepted silently

File: `global_calibrator.py:33-44` (`__post_init__` only checks `prob_x`, `er_x`).

Mechanism: `np.interp` requires monotone x, which is enforced. But there is no
check that `er_y` / `prob_y` are monotone non-decreasing. A calibrator artifact
with a non-monotone `er_y` (e.g. a mid-domain dip below zero) is loaded without
complaint and produces a non-monotone μ surface — two raw scores with `a<b` can
yield `μ(a)>μ(b)`, and the sign can flip mid-domain. The class docstring claims
"Two monotone interpolation heads" but nothing enforces it.

Reproduction:
```
GlobalPanelCalibration(er_x=[-0.1,0,0.1], er_y=[0.05,-0.02,0.08])  # accepted, no error
  expected_return(-0.05)=0.0150   expected_return(0.05)=0.0300   (non-monotone, sign-flipping)
```

All current prod artifacts happen to have monotone y (checked), so this is a
latent contract gap, not an active miscalc — but it is the exact silent failure
mode F3 relies on and there is no guard.

Fix direction: in `__post_init__` (and `load`), assert `np.diff(er_y) >= -tol`
and `np.diff(prob_y) >= -tol`; downgrade to a logged warn only behind an
explicit opt-in.

---

## F7 — [MED] Regime calibrators are 10d-native but linearly scaled ×6 to the 60d rotation horizon; native horizon mixed with pooled 60d

Files: `global_calibrator.py:98-115` (`_native_lookahead_days`, `_scale_expected_return_to_horizon`); task side `_calibrator_native_horizon_days:1947-1953`.
Artifacts: `panel-calibration-BULL_CALM.json` / `-CHOPPY.json` metadata `lookahead_days=10`.

Mechanism: `_native_lookahead_days` reads `lookahead_days` (=10 for the regime
heads). `expected_return(raw, horizon_days=60)` then multiplies by `60/10 = 6`.
Reproduced: BULL_CALM `ER(0.05)` native `0.02595` → scaled `0.15568` (15.6% over
60d). The pooled hf calibrator is natively 60d (`lookahead_days_used=60`). So a
config that loads regime heads alongside the pooled fallback mixes a 10d-native
head (linearly ×6) with a 60d-native head under one rotation horizon — different
effective scaling per regime, and the ×6 linear drift extrapolation will push
ER toward / past the 0.20 sanity bound.

Fix direction: refit regime calibrators on the 60d ER label (matching pooled),
or store `lookahead_days_used` consistently and forbid loading heads whose native
horizon differs from the pooled head without explicit acknowledgement.

---

## F8 — [MED] `sigma_horizon_days: null` silently NaNs Kelly for every candidate

Files: `_kelly_sigma_horizon_days:3055-3063`, consumed `ApplyKellySizingTask:3104,3154-3155`.

Mechanism: `kelly_cfg.get("sigma_horizon_days", 252.0)` returns the *value*
`None` when the key is present with a null (which is the case in
`backtesting/renquant_104/strategy_config.json`). `float(None)` raises
`TypeError` → function returns `nan` → `_kelly_with_reason` short-circuits with
`kelly_zero:sigma_horizon_invalid` for every candidate. A present-but-null key
is treated worse than an absent key (absent → 252.0). This is a footgun: a
single `null` in config zeroes all Kelly targets with a non-obvious reason.

Reproduced:
```
sigma_horizon_days=None -> nan     key absent -> 252.0     =60 -> 60.0
```

Caveat: `runs.alpaca.db` shows 0 all-time `sigma_horizon_invalid` blocks, so the
*live* runs did not hit this (their effective config differs from the checked-in
file, or upstream mu gates fire first). It is a latent fragility, not an active
outage — hence MED.

Fix direction: coalesce `None` to the default (`raw = kelly_cfg.get(...) or
252.0` or an explicit `if raw is None`), and treat null as "use μ horizon".

---

## F9 — [LOW] PatchTST distributional-head σ is computed then discarded

File: `hf_patchtst_scorer.py:313-316` stores `self._last_sigma`; grep shows no
reader anywhere in `kernel/panel_pipeline/`.

Mechanism: When the HF PatchTST checkpoint has a distributional head, the
forward pass returns a per-ticker `scale` (σ) which is captured into
`_last_sigma` but never propagated onto candidates/holdings. Kelly σ comes only
from NGBoost or the realized-vol fallback. The model's own, arguably
better-calibrated, uncertainty estimate is dead. Combined with F4 this is
relevant: the discarded σ is already on the model's score scale, not annualized.

Fix direction: when present, surface `_last_sigma` as a candidate `sigma`
source (with an explicit horizon contract) ahead of the realized-vol fallback.

---

## F10 — [LOW] Post-calibration soft-check `ok` result is ignored — hard-fails only log

File: `job_panel_scoring.py:2312-2322` calls `soft_check_score_series(...)` and
discards the returned `CheckResult`. `model_contract.soft_check_score_series`
can flag a HARD FAIL (collapsed std, out-of-[0,1] range) but the task never
inspects `res.ok`, so a genuinely degenerate post-calibration `rank_score`
series only emits a log line and continues into selection. The richer
saturation/abstain logic that follows (`2337-2395`) is the real guard; the
soft-check call here is effectively decorative.

Fix direction: either remove the dead call or wire `res.ok` into the
abstain/fail-closed decision so the contract has teeth.

---

## Checked and found NOT a bug (so the next pass can skip)

- **ROC orientation** (`alpha158_features.py:138,334` `c.shift(n)/c`): matches the
  training builder `RenQuant/scripts/build_alpha158_qlib.py:230-231` and Qlib's
  exact `Ref($close,n)/$close` convention. Inverted vs textbook ROC but
  train/inference-consistent. Intentional.
- **alpha158 `compute_alpha158_at` vs `compute_alpha158_frame`**: reproduced the
  pinned invariant over synthetic OHLCV — 0 divergences across all 158 features
  (incl. RSQR/RESI NaN paths). Consistent.
- **Volume zero-fallback** (`alpha158_features.py:203-210,321-323`): both at/frame
  use the rolling-mean fallback with the same window; no 1e16 blowup. Consistent.
- **Probability clamp direction** (`global_calibrator.py:54-57`): clamps to
  `prob_y[0]/[-1]`; for the live hf head `prob_y[0]=0.307 < 0.5`, so below-floor
  scores correctly clamp below the buy probability. Correct.
- **Score/calibrator fingerprint binding** (`_assert_calibrator_matches_scorer`,
  `job_panel_scoring.py:1868-1907`): binds on `model_content_fingerprint`,
  fail-closed on mismatch. Sound (the mutable-metadata exclusion list at
  `panel_scorer.py:43-86` is deliberate and documented).
- **`er_y` ±0.20 load clip** (`global_calibrator.py:167-174`): present and works;
  it bounds the Kelly numerator. The `recent-12mo`/`pre-clip` artifacts with
  `er_y` up to +1.0 are clipped on load.
