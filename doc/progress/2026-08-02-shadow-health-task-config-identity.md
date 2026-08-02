# Shadow-health records carry the strategy-config identity of the emitting task (#256)

STATUS: complete on this branch — contract-additive fields on every emitted
record + 4 deterministic regressions; behavior-inert for every existing
reader (no field renamed/repurposed, no schema bump, verdict logic untouched).
NEXT: codex review + merge; then the orchestrator-side companion (scoping the
sentinel's task-level reads to the pinned config's identity) can consume the
stamp; rides the normal pin cadence.

WHAT: implements issue hallovorld/renquant-pipeline#256. The shadow-health
sink (`logs/shadow_scorer_health.jsonl`) receives records from MULTIPLE
invocations per session — the main daily run's per-lane records, then the
shadow_blend companion profile's task-level `state=no_shadow_models` record,
always AFTER. That task-level record carried `config_fingerprint: null` and
no other identity of the strategy config it ran under, so a reader could not
tell "the MAIN config dropped all shadow lanes" from "a different profile
that legitimately has none also wrote here" — and the orchestrator sentinel's
last-record-per-date-wins task state could fire the disappeared-from-config
clause off ANOTHER profile's record.

- `src/renquant_pipeline/kernel/panel_pipeline/shadow_health.py` (the
  canonical three-consumer contract module):
  * `STRATEGY_CONFIG_PATH_KEY = "_strategy_config_path"` — the config key
    the umbrella live runner already stamps with the RESOLVED path of the
    strategy config file it loaded (`live/runner.py`, set alongside
    `_strategy_dir` / `_strategy_config_name`);
  * `task_config_identity(config)` → `(task_config_path,
    task_config_sha256)`: reads that stamped path and hashes the file with
    the EXISTING canonical `content_digest` recipe (`sha256:<16 hex>`, the
    same convention the record's artifact `content_sha256` already uses —
    no second hashing convention invented). FAIL CLOSED: when the runner
    did not stamp a path, returns `(None, None)` rather than guessing
    `<strategy_dir>/strategy_config.json` — a guessed default could stamp
    the main config's identity onto a companion profile's record,
    recreating the exact false-attribution vector this field kills;
  * `new_shadow_health` gains the two fields (default `None`, present on
    every record so the schema stays stable for the sentinel parser), with
    an inline comment distinguishing them from the per-lane
    `config_fingerprint` — the ARTIFACT's training-config fingerprint, a
    different object whose meaning is untouched.
- `src/renquant_pipeline/kernel/panel_pipeline/shadow_scoring.py`
  (`ApplyShadowScoringTask.run`): computes the identity ONCE per run and
  passes it into BOTH `new_shadow_health` construction sites — the
  task-level `_skip_record` path (disabled / no_shadow_models /
  no_candidates) and the per-lane loop — so EVERY record the task writes
  carries the stamp.
- `tests/test_shadow_scorer_health_record.py`: `_ctx` fixture gains a
  `config_path` kwarg mirroring the runner's stamp; 4 new regressions in
  the existing suite's idiom (real sink writes via the REAL producer path):
  (a) the task-level `no_shadow_models` record carries path + digest;
  (b) per-lane records carry the same stamp while `config_fingerprint`
  keeps its artifact meaning; (c) the measured live scenario — main run
  with a lane, then a companion with none, same sink — yields records
  distinguishable by BOTH fields; (d) an unstamped runner leaves the
  fields `None` even when a guessable `strategy_config.json` exists.

Schema-version precedent followed: additive fields do NOT bump
`shadow_scorer_health.v1`. Precedent commits on these files: b69f209
(`trained_date` / `lookahead_days` added, v1 kept), 12da2cf
(`n_scored_total` added, v1 kept), and the `STATE_NOT_YET_PUBLISHED`
comment documenting the policy ("additive … no schema bump" — the deployed
sentinel's `is_valid_v1_record` ignores unknown fields).

WHY/DIR: GOAL-1 fail-closed discipline — a record that cannot be attributed
to the config that produced it is not evidence about that config. The
false-attribution vector was measured live one day before becoming reachable
(`previous_primary` lands in the watched set; any skipped day leaves the
momentum lane with no per-lane record in the window, and the day's surviving
task state is the companion profile's `no_shadow_models`).

EVIDENCE:
  artifact:      tests/test_shadow_scorer_health_record.py (4 new
                 regressions + the 33 existing tests) +
                 tests/test_task_level_skip_names_itself.py (5, unchanged)
  prod or exp:   exp — contract-additive only; existing fields keep their
                 meaning, existing readers parse unchanged (the deployed
                 sentinel requires specific fields and ignores extras);
                 no production path written
  existing data: issue hallovorld/renquant-pipeline#256 (measured 2026-08-02
                 on the live sink: main run 2026-07-31-live-381747dd +
                 companion 2026-07-31-live-8aa713e5 write the same file);
                 codex on #240 (the alarm class the task-level record
                 exists to make legible)
  best-known?:   yes — reuses the runner's existing `_strategy_config_path`
                 stamp and the contract module's existing `content_digest`
                 recipe; the alternatives (re-deriving a path, or a second
                 hash convention) each add a divergence surface for zero
                 additional information
  scope:         "this is tests/test_shadow_scorer_health_record.py (37
                 tests) + tests/test_task_level_skip_names_itself.py (5) +
                 the full pipeline suite, exp path, vs baseline =
                 origin/main dff3cbe"

  Measured counts: touched files **42 passed** (37 + 5; 4 new)
  [VERIFIED — pytest -q on both files, this branch, 2026-08-02]. All 4 new
  tests **fail on the pre-fix source** (stash src/, rerun: 4 failed) —
  valid regressions [VERIFIED — same session]. Full suite: **2 failed,
  2380 passed, 9 skipped** — the same 2 pre-existing
  `test_replay_d6_conventions` pin-platform byte-identity failures
  reproduced UNCHANGED on clean origin/main dff3cbe in this environment,
  zero regressions [VERIFIED — make test, this worktree, pre- and
  post-change].

NEXT: orchestrator companion issue — `rq104_shadow_scorer_sentinel` scopes
`read_task_level_states` to the pinned config's `task_config_sha256` (it can
compute the expected digest with the same `content_digest` recipe this
contract module exports).
