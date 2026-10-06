# Shadow health: a frozen forward readout is stale by design, not a fault   (PR #309)

STATUS:    delivered — G-A (stop the standing page by fixing its cause):
           the certified top-decile classifier lane no longer pages
           DEGRADED every session for being exactly what it must be.
WHAT:      `kernel/panel_pipeline/shadow_health.py` gains a registry
           `FROZEN_FORWARD_READOUTS` with ONE entry — lane
           `topdecile_clf_blend_leg`, certified artifact digest
           `1e644354e0981f47`, window 2026-07-27 → 2027-03-31 inclusive,
           authority pipeline#213 — and `finalize_shadow_health` step 1b:
           when the record's OBSERVED `content_sha256` AND the config-pinned
           `expected_content_sha256` both equal that digest, the lane name
           matches, and `run_date` is inside the window, the freshness
           tokens (`cutoff_lag_*`, `trained_Nd_limit_*`, `stale_*`,
           `no_declared_lookahead_single_axis`) are removed from `reasons`
           and written to `health["frozen_forward_readout"].freshness_suppressed`
           together with the window, `days_left` and the authority. Every
           other fault class — identity mismatch, missing provenance,
           future/missing/unparseable dates, low coverage, no scores — is
           untouched. `staleness_days` / `trained_age_days` stay in the
           record. No sentinel change: the orchestrator sentinel passes the
           producer's `status` through on structured records
           (`rq104_shadow_scorer_sentinel.classify`, lines 490–499) and
           derives staleness only on the DB fallback this lane never uses.
           9 new tests (`tests/test_shadow_health_frozen_forward_readout.py`):
           the live 09-03 record shape → OK with the two suppressed tokens
           recorded; same numbers on another lane → DEGRADED; swapped
           artifact → DEGRADED + `content_sha256_mismatch`; config that stops
           pinning → DEGRADED; inclusive window edges + self-expiry; low
           coverage / missing fingerprint / no scores still faults; provenance
           defects not suppressed; digest-form agreement (16-hex pin, bare
           prefix, <16 hex never matches).
WHY/DIR:   The two-axis freshness rule (GOAL-6 decision A) is right for a
           lane that is supposed to be retrained. This lane is the CONFIRMED
           blend objective's clf leg, frozen since its first shadow session
           2026-07-27 for a 60-session INFO read and a 120-session GATE
           read (pipeline#213; strategy-104 config `_2026_07_26_role`:
           "governed by pipeline#213 frozen forward readout"); retraining it
           would void the readout. So from ~day 29 the sentinel has paged
           "NOT ACTIONABLE / DEGRADED … trained_Nd_limit_28d" about a lane
           whose whole value is that it is NOT retrained, and since
           2026-08-30 also `cutoff_lag_Nd_over_112d`. That page cannot be
           acted on, so it trains the operator to ignore SHADOW SCORER
           DEGRADED — which also carries the REAL momentum-lane staleness
           (`momentum_residual_v0 stale 32d`, the 08-31 ledger truncation).
           Binding the exemption to the artifact digest + config pin + a
           calendar window keeps it honest: swap the file, drop the pin, or
           let the window lapse and the standing rule returns with no code
           change — the same self-expiring shape as the RFC#210 A4-T1
           license. Design choice recorded: a per-lane config key
           (`frozen_until`) would have needed a served-config write under
           LONG row 2; the digest-bound code registry needs none.
EVIDENCE:  artifact:      `RenQuant/logs/rq104_shadow_scorer_sentinel.log` — `[topdecile_clf_blend_leg] … DEGRADED: 2 consecutive session day(s) — 2026-09-02 [degraded: cutoff_lag_127d_over_112d(floor_84d+slack_28d), trained_36d_limit_28d]; 2026-09-03 [degraded: cutoff_lag_128d_over_…]` [VERIFIED — read 2026-09-03 between 18:36 and 18:50 PDT]; served config (pinned strategy-104 `configs/strategy_config.json`) shadow entry: `artifact_path artifacts/shadow/panel-clf.top-decile.fwd60.json`, `expected_content_sha256 sha256:1e644354e0981f47`; the file on disk: trained 2026-07-28, cutoff 2026-04-28, sha256 prefix `1e644354e0981f47` [VERIFIED — read-only json + hashlib, same window]
           prod or exp:   prod shadow-health record for one lane (state/reasons only); no scoring, config, artifact, or sentinel change
           existing data: `tests/test_shadow_health_frozen_forward_readout.py` 9 passed + `tests/test_shadow_health.py` 8 + `tests/test_shadow_scorer_health_record.py` = 54 passed [VERIFIED — 2026-09-03 between 18:50 and 18:55 PDT]; full pipeline suite in the worktree: 2802 passed, 11 skipped, 2 failed — the 2 failures are `tests/test_replay_d6_conventions.py::TestDefaultModeUnchanged::{test_default_evidence_matches_pre_change_pin,test_default_evidence_byte_identical_on_pin_platform}`, which fail IDENTICALLY on origin/main faf1416a with this change stashed (platform-pinned replay evidence; pre-existing, unrelated) [VERIFIED — 2026-09-03 between 18:40 and 18:42 PDT]
           best-known?:   n/a — ops truth; no model claim. The readout schedule (INFO ~mid-Nov 2026, GATE ~Feb 2027) is pipeline#213's; `until = 2027-03-31` is that schedule plus one month of slack [DERIVED]
           scope:         "this changes how ONE lane's freshness tokens are classified while its certified artifact is served, until 2027-03-31; it does not change any threshold, any other lane, the sentinel, or the config"
NEXT:      umbrella pin advance (renquant-pipeline → this merge) + snapshot →
           live ff-only → `subrepo_assemble --sync` → the next 13:55 daily run
           writes the clf lane's record with `state: ok` and
           `frozen_forward_readout.freshness_suppressed = [cutoff_lag_…,
           trained_…]`; the 06:xx sentinel then reports the lane OK and the
           SHADOW SCORER DEGRADED page, when it fires, is about the momentum
           lane only (which heals with the 2026-09-05 refit). When the
           readout completes (GATE read), retire the registry entry in the
           same PR that acts on the verdict.
