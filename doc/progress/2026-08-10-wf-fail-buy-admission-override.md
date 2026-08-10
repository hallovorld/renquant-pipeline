# wf-gate: governed operator override for a WF-FAIL buy admission

STATUS:    Implemented. MECHANISM ONLY — no authorization is granted, no
           strategy_config is written by this PR. New module
           `kernel/wf_fail_override.py` + two enforcement points wired
           (preflight P-WF-GATE passed=False branch; scoring-path
           RegimeModelAdmissionTask). `tests/test_wf_fail_override.py` 37
           green. Full suite 2590 passed, 9 skipped, 2 failed — the 2 are
           `test_replay_d6_conventions.py::TestDefaultModeUnchanged` and were
           ALREADY failing on clean `origin/main` (a9d747a), unrelated to this
           change (baseline probe below). Behaviour-invariant with no
           authorization block.

WHAT:      The WF gate stamps `metadata.wf_gate_metadata.passed`. `passed is
           False` is the gate's STRONGEST negative (candidate evaluated and
           rejected — e.g. `benchmark_ok=False`, ΔSharpe −0.479 vs SPY);
           P-WF-GATE HARD-fails a full/buy run on it. This PR adds the ONLY
           sanctioned exception at that branch: an explicit "I-accept-the-risk"
           operator authorization in strategy config, DISTINCT from and more
           stringent than the existing `diagnostic_only` override.

           New `evaluate_wf_fail_override(wf, config, *, scorer_content_sha,
           now=None)` reads a SEPARATE config block:

             wf_gate.wf_fail_buy_admission = {
               authorized, operator, authorized_at, expires,
               scorer_model_content_sha256, wf_reason_acknowledged, reason
             }

           mirroring `kernel/diagnostic_only_override.py`'s structure and its
           governance properties — all fail-closed: absent / non-dict /
           malformed field / unparseable date / expired / missing-or-mismatched
           scorer hash / hash-impl unavailable → the refusal STANDS; a defect is
           logged WARNING and never widens access. `expires` is REQUIRED (the
           day after, the refusal auto-returns). The authorization names the
           schema-v1 content hash of the ONE scorer it covers
           (`renquant_common.model_fingerprint.model_content_sha256`); a
           re-promoted / retrained artifact does not inherit it. On success the
           full record + computed hash + acknowledged reason is returned as
           `provenance` for the run bundle.

           EXTRA STRINGENCY vs diagnostic_only — `wf_reason_acknowledged` MUST
           byte-equal the artifact's actual `wf_gate_metadata.wf_reason`, so the
           operator provably saw the SPECIFIC failure they override. A stale
           authorization written for a different failure (ΔSharpe moved,
           benchmark_ok flipped, regimes changed) does not admit.

           DISTINCT FROM diagnostic_only — different config KEY
           (`wf_fail_buy_admission` vs `diagnostic_only_buy_admission`). The
           diagnostic_only path is untouched and is consulted ONLY on the
           `passed=True` branch, so a diagnostic_only authorization can NEVER
           admit a `passed=False` artifact, and vice-versa.

           Wiring, two enforcement points (mirroring diagnostic_only):
           - Preflight `gate.py::WfGateMetadataTask._fail_with_evidence`
             (passed=False): consulted ONLY after the sell-only and RFC #210
             branches (both unchanged). If authorized → HARD ok=True with
             `details.wf_fail_override`; else the existing HARD fail stands (a
             present-but-rejected authorization adds `wf_fail_override_rejected`
             and a message suffix; an ABSENT block changes nothing).
           - Scoring path `job_panel_scoring.py::_wf_fail_admission`, wired into
             `RegimeModelAdmissionTask.run` next to `_diagnostic_only_admission`
             so a bypassed preflight is still caught. ONE deliberate asymmetry
             vs diagnostic_only: it does NOT introduce a new UNCONDITIONAL
             passed=False block — with no authorization it passes through. The
             unconditional passed=False refusal is P-WF-GATE's and RFC #210's
             job (RFC #210 itself has no scoring-path twin), and the live book
             is currently an RFC #210-served passed=False artifact; a new
             unconditional block here would refuse the live book and break
             behaviour invariance. What it DOES enforce is THIS PR's new
             surface: a present-but-rejected wf_fail authorization is refused
             here too (fail-closed defence-in-depth), a valid one admits with
             provenance.

WHY/DIR:   Direction is toward MORE stringency, not less. The operator wants a
           governed "buy anyway" for a hard gate fail WITHOUT deleting the gate.
           A hard `passed=False` is a stronger negative than a diagnostic-only
           stamp, so it demands a SEPARATE, DISTINCT, more-stringent
           authorization — hence the distinct key + the wf_reason byte-ack that
           diagnostic_only does not require. The authorization RECORD lives in
           strategy config OUTSIDE the model-relevant config-fingerprint
           projection (`renquant_common.config_consistency._model_relevant_fields`
           hashes only watchlist / panel_ltr / sector maps / benchmark /
           resolution flags), so adding or expiring it never invalidates artifact
           config-consistency stamps — pinned by a test.

           The real passed=False `wf_reason` this override is written against,
           measured 2026-08-10 on
           `RenQuant/backtesting/renquant_104/artifacts/prod/panel-ltr.alpha158_fund.weekly_20260804T200020Z.staging.json`:
           `"FAIL: absolute_ok=True, benchmark_ok=False, regime_ok=False; mean
           Sharpe +0.602, 3/3 cuts > 0; SPY mean Sharpe +1.081, ΔSharpe -0.479,
           beat SPY Sharpe 1/3, beat SPY APY 0/3; benchmark-lag
           regimes=['HIGH_CALM', 'LOW_SPIKED']"`. The reason string lives under
           `wf_reason` (present on 80/80 stamped artifacts), NOT `reason`, which
           is what the byte-ack compares against.

EVIDENCE:  `[VERIFIED — pytest via renquant-pipeline/.venv (py3.11) with
           PYTHONPATH=worktree/src:../renquant-common/src:..., 2026-08-10]`

           - `tests/test_wf_fail_override.py` — 37 cases:
             * validator fail-closed: absent, non-dict, each malformed field
               (incl. `wf_reason_acknowledged` empty/blank), unparseable dates,
               expired (+ expiry-date-itself-valid boundary), wrong scorer hash,
               no scorer identity → `scorer_hash_unavailable`, wf_reason
               mismatch, wf_reason absent-on-artifact, happy-path provenance,
               payload-hash helper == renquant_common, helper quiet on None/{}.
             * DISTINCTNESS: a `diagnostic_only_buy_admission` block yields
               `absent` under `evaluate_wf_fail_override` and vice-versa; and
               through the LIVE preflight task a diagnostic_only authorization
               on a passed=False artifact still HARD-fails (`wf_fail_override`
               absent from details).
             * preflight integration: BEHAVIOUR INVARIANCE — passed=False with
               NO block hard-fails with the EXACT message asserted byte-for-byte
               and no `wf_fail_override*` keys in details; rejected (expired)
               names the reason in message + details; valid admits HARD ok=True
               with provenance + "I-accept-the-risk"; wrong scorer blocks;
               sell-only unchanged.
             * scoring integration: passed=False + no block passes through
               (details == {}); passed=True untouched; valid admits with
               provenance; wrong-scorer / wf_reason-mismatch / expired each
               block with `wf_fail_override_rejected` reason; the sibling
               diagnostic_only guard ignores a plain passed=False (guards are
               distinct).
             * config-fingerprint invariant: `fingerprint_config` equal with/
               without the block; `wf_gate` not in `_model_relevant_fields`.

           - Reverse/vacuity check: the behaviour-invariance test asserts the
             hard-fail MESSAGE string literally, so the wf_fail branch cannot
             silently alter the no-block path; the distinctness tests key on the
             SEPARATE config key, so a diagnostic_only authorization cannot
             satisfy this override by construction.

           - Twin guard: the KERNEL `RegimeModelAdmissionTask` digest was
             re-pinned in `twin_pairs.json` (kernel_sha256
             3b4c2d11…→0c898b93…, public_sha256 unchanged) with a
             `_repin_2026_08_10` stated reason — the public twin
             (`panel_scoring.RegimeModelAdmissionTask`) is a genuinely different
             implementation (`evaluate_model_admission`, no
             diagnostic_only/wf_fail admission), so a KERNEL-ONLY re-pin is
             correct. New kernel stem `wf_fail_override` added to
             `OWNED_KERNEL_STEMS`. `test_twin_pairs*` + `test_kernel_ownership_
             contract` 31 green.

           - Baseline probe for the 2 remaining suite failures: a detached
             `origin/main` (a9d747a) worktree runs
             `test_replay_d6_conventions.py::TestDefaultModeUnchanged` → 2
             failed, 94 passed BEFORE any change here. They are pre-existing and
             out of scope.

NEXT:      1. MECHANISM only — this ships NO authorization. To actually admit
              buys on a passed=False artifact the operator supplies, in a
              SEPARATE reviewed change: (a) a `wf_gate.wf_fail_buy_admission`
              block in the renquant-strategy-104 config (with a live
              scorer_model_content_sha256 and the byte-exact current wf_reason),
              and (b) a LONG-ledger row recording the authorization (analogous
              to the diagnostic_only row 2b for s104#97). Neither is in this PR.
           2. merged-is-not-deployed: reaches the daily run only after the
              umbrella pin advances and the runtime checkout syncs. The live
              artifact is presently RFC #210-served (passed=False), so the RFC
              #210 path already admits it today; wf_fail is the path for a hard
              fail with NO freshness license.
           3. Legacy twin `kernel/preflight.py::_check_wf_gate_metadata` is NOT
              wired (mirrors the diagnostic_only precedent, which also lives in
              gate.py only). If the legacy monolith is ever the resolved live
              path for a passed=False override, it needs the same wiring.
