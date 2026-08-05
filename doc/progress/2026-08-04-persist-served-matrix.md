# 2026-08-04 — the served feature matrix now survives the run that used it (orch#703)

## The gap, and why it stopped being theoretical today

`build_inference_matrix` produces the matrix that decides every trade, and
nothing ever wrote it down. `job_panel_scoring` reads it as `ctx._panel_matrix`,
an in-memory attribute on the run context — so the input that produced today's
scores ceases to exist when the process ends `[VERIFIED — orch#703, 2026-08-01]`.

That was a GOAL-4 blocker on paper. Today it became a daily one: the GOAL-9
fleet has five lanes scoring the same universe and picking DIFFERENT names
`[VERIFIED — 2026-08-04 run logs]` — prod bought NVDA/GOOG/WELL/VLO, RC bought
AMZN, RSs bought SPG, RCS bought BWXT. Nothing on disk can explain a single one
of those differences the next morning, and the divergence compounds every day.

## What lands

`kernel/panel_pipeline/served_matrix_sink.py` + `PersistServedMatrixTask`,
appended LAST in `PanelScoringJob`:

```
<strategy_dir>/logs/served_matrix/<YYYY-MM-DD>/<lane>__<run_id>.parquet
<strategy_dir>/logs/served_matrix/<YYYY-MM-DD>/<lane>__<run_id>.json
```

- **Last on purpose.** The raw scorer output alone does not explain a buy.
  Placed after calibration, NGBoost and Kelly, the parquet carries `rank_score`
  as it actually decided, plus `mu`/`sigma`/`kelly_target_pct` and the
  candidate/holding role flags, next to every served feature column.
- **The sidecar makes it readable later**: scorer kind, content digest, config
  fingerprint, trained date, per-component identity for a blend, and the run/lane
  identity. Absent fields are recorded as `null` — an absent field must read as
  absent, never as a default that looks like a measurement.
- **Sink resolution mirrors the proven `shadow_health` convention**
  (`_strategy_dir`, or an explicit `served_matrix.dir` override). Without either,
  it SKIPS rather than scatter parquet into a bare cwd.
- **run_id / lane use the existing idioms**, not new keys:
  `task_decision_ledger.py:56`'s `run_id or _run_id or "<date>-unscoped"`, and
  `ctx.broker_name` (the broker-isolation tag, `context.py:34`).

## It cannot break a run

This is a logging path. `build_records`/`write_served_matrix` raise only
`ServedMatrixSinkError`, the task catches EVERY exception, logs a warning and
returns `None` (continue). Tests cover a missing matrix, a raising writer, a
scorer object with no metadata at all, no strategy dir, and an explicit disable.

Durability: parquet and JSON are each written to a `.incoming` sibling and
`os.replace`d, and the **JSON lands last**, so its presence means the pair is
complete. A failed parquet write leaves no torn file, no sidecar, and no
`.incoming`.

## The twin pair stays intentionally asymmetric

The public `PanelScoringJob` (`renquant_pipeline/panel_scoring.py`) is the
intraday/frozen-score chain: no NGBoost/Kelly/QualityFloor stage, and its matrix
lives at `ctx.panel_feature_matrix` — a dict that `_StubFrozenFeatureMatrixTask`
fills with EMPTY per-ticker dicts. Persisting there would write a file that looks
like served-input evidence and contains none. The kernel pin is re-emitted with
that reason recorded in `twin_pairs.json`, per the guard's own instruction
("apply it to both or re-pin with a stated reason").

## Cost

~145 rows × ~172 float columns ≈ 100 KB per lane-run; six lanes daily ≈ 0.6 MB/day,
≈ 0.2 GB/year `[推导 — from the live watchlist and feature count]`. **Nothing here
deletes anything.** Retention is an operator decision, not a side effect of a
logging path.

## Not covered

The comparison this unblocks (GOAL-4's real-served-panel test) needs N sessions
of accumulation before it can run. This PR buys the first day of it; it does not
make any claim about the models.

Suites: 11 new tests · 2464 passed, 8 skipped (full pipeline).
