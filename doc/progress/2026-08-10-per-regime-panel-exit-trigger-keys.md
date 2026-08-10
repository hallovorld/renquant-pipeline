# 2026-08-10 — CrossSectionalPanelExitTask reads the BEAR-exit prereg's two per-regime trigger keys

STATUS:   IMPLEMENTED + TESTED (orch#962 blocker B1). Full suite 2506 passed /
          12 skipped / 2 pre-existing unrelated failures in
          `tests/test_replay_d6_conventions.py` (confirmed identical on the
          unmodified `origin/main` head via `git stash` in the same worktree)
          `[VERIFIED — make test]`. Module suite 13 passed
          `[VERIFIED — python -m pytest tests/test_lift_sell_path.py -q]`.
          `ruff check` on both changed files: only the one E702 that already
          exists on `origin/main` (line pre-dates this PR).

WHAT:     `CrossSectionalPanelExitTask` now resolves its two AND-rule trigger
          knobs per-regime:

          * `xs_panel_percentile_floor_by_regime`
          * `mu_sell_ceiling_by_regime`

          via a new module-level `_by_regime_trigger_value()` that mirrors the
          EXISTING `min_holding_days_by_regime` pattern
          (`soft_exit_guards._configured_min_days`) — resolution order: exact
          `regime` entry → explicit `default`/`_default` key → the flat scalar
          (with its scalar default). Non-dict / empty maps are ignored (flat
          scalar used), exactly like the existing key. Value parsing stays in
          the task's existing scalar path: a malformed resolved value raises
          inside the existing `try/except (TypeError, ValueError)` and the
          task skips the bar (no false exit) — the same behavior a malformed
          scalar exhibits today. The regime used is `ctx.regime` (the day's
          regime), matching the prereg's day-keyed regime series.

WHY/DIR:  The BEAR-exit prereg (orchestrator
          `doc/design/2026-08-08-bear-exit-prereg.md` §2) freezes a candidate
          amendment carrying these two NEW keys; §4 item 2 authorizes exactly
          this pipeline change ("reading `_by_regime` keys,
          default-preserving: normal PR + codex review; behaviour-invariance
          regression mandatory"). The task previously read only the scalar
          forms, so the frozen amendment was not runnable (orch#962 B1).
          Frozen fallback semantics honoured verbatim: "an absent regime key
          resolves to `default`, and a config with ONLY the old scalar keys
          behaves byte-identically to today (regression required)". No config
          change ships here — the live `strategy_config.json` amendment
          remains operator-grant-gated (prereg §4 item 3).

EVIDENCE:
artifact:      `src/renquant_pipeline/kernel/pipeline/task_panel_conviction_xs.py`
               (`_by_regime_trigger_value` + the two resolver call sites +
               config docstring), `tests/test_lift_sell_path.py` (7 new tests:
               exact-regime override fires in BEAR; absent regime → `default`
               entry; `_default` alias; `{default: scalar}` maps ≡ scalar-only
               run; malformed map ignored like the existing key; malformed
               per-regime value skips like a malformed scalar; behavior
               invariance without the new keys across all four regimes). Each
               parity test pins the EXISTING `min_holding_days_by_regime`
               semantics via `soft_exit_guards._configured_min_days` FIRST,
               then asserts the new keys match.
prod or exp:   prod code path (the live daily exit task), default-preserving:
               with no `_by_regime` maps configured — the pinned production
               config's current state for these two knobs — the resolver
               returns `cfg.get(key, scalar_default)`, the byte-identical
               expression the task used before. Behavior changes ONLY when a
               config explicitly carries one of the two new maps, which no
               production config does yet.
existing data: the reachability measurement behind the prereg (43 days × 200
               rows, zero fires; orchestrator
               `doc/research/data/2026-08-08-bear-exit-reachability-rows.csv`)
               is unaffected — this PR changes no verdict and re-measures
               nothing. Test expectations (threshold idx arithmetic,
               `round(10*0.35)=4` etc.) are hand-derived from the task's own
               unchanged threshold code and asserted against full reason
               strings.
best-known?:   yes for the resolution pattern — it is a verbatim mirror of the
               repo's established per-regime pattern
               (`_configured_min_days`, itself modeled on
               `_qp_admission_gate_value`), chosen BY the prereg over
               inventing new semantics. The min-days-specific zero-floor
               warning is not mirrored because its hazard (fall-through
               silently disabling a guard) has no analog here: the scalar
               fallback IS current production behavior.
scope:         one new module-level function + two call-site lines + one
               docstring block in one task file; 7 tests appended to the
               task's existing test module. No config, pin, artifact, or
               schema change; no other task touched; no write to any
               production path (work done in an isolated worktree).

NEXT:     The prereg's §3/§3.1 evaluation run (research-only, no grant needed)
          can now execute against a branch/pin carrying this change; any live
          config amendment stays operator-grant-gated per prereg §4 item 3.

## REVERT

Delete `_by_regime_trigger_value` and restore the two call sites to
`float(cfg.get("xs_panel_percentile_floor", 0.20))` /
`float(cfg.get("mu_sell_ceiling", 0.0))` (dropping the `regime =` line),
remove the docstring's per-regime block, and delete the appended test section
in `tests/test_lift_sell_path.py` (everything from the per-regime banner
comment down) plus the four imports it added at the top. No other file
changes.
