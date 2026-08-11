# Progress — P-BROKER-CONNECT bounded retry (fail-closed preserved)

Date: 2026-08-11
Deliverable: bounded retry in the P-BROKER-CONNECT preflight so a single
transient Alpaca network blip no longer aborts the whole intraday cycle. Both
the runtime task and the legacy bridge, sharing one retry body.

## The bug (verified 2026-08-11)

The `intraday_104` 07:01 run aborted:

```
✗ P-BROKER-CONNECT [HARD] broker connect failed:
  HTTPSConnectionPool(host='api.alpaca.markets', port=443):
  Read timed out. (read timeout=None)
```

The check did ONE `broker.connect()` + `broker.get_account_value()`; any
exception → HARD False, no retry. So one transient blip aborted the cycle (no
orders), recovering only on the next scheduled run ~12 min later.

## What

- New shared body `_attempt_broker_connect(broker, *, max_attempts=3,
  backoff_seconds=2.0)` in `kernel/preflight.py`: retries
  `connect()`+`get_account_value()` up to `max_attempts` with a short backoff,
  returns HARD **True** on the first success (message names the attempt count
  when it took more than one), and returns HARD **False** only after ALL
  attempts fail — the message names the attempt count and surfaces the last
  error. **Fail-closed is preserved**: a genuine outage still refuses to trade.
- `_check_broker_connect(broker, *, max_attempts=3, backoff_seconds=2.0)` — the
  None/dry-run soft-skip branch is unchanged; on a real broker it delegates to
  the shared body. The new kwargs are optional with defaults, so the single
  existing call site (the `ALL_CHECKS` tuple, invoked as `check(broker)`) is
  unaffected. [VERIFIED call site — `ALL_CHECKS` line ~1855; no other caller.]
- `BrokerConnectTask.check` (the RUNTIME path — `build_preflight_pipeline()`
  registers it, `preflight_pipeline/pipeline.py`) now calls the SAME
  `_attempt_broker_connect` body. This closes the pipeline twin-task trap
  (memory: `pipeline-has-twin-task-implementations`): the legacy `_check_*`
  and the runtime Task can no longer drift on retry semantics.
- `import time` added to `kernel/preflight.py`.

Bounds: **3 attempts, ~2s backoff.** Worst-case added latency ≈
`(3-1) × (read_timeout + backoff)`; with renquant-execution's bounded
`(connect=5s, read=10s)` timeout that is ≈ 2×(10+2) = 24s, comfortably under
the ~12-min intraday cadence.

## DEPENDENCY (documented per task)

These retries stay well under the ~12-min cadence ONLY because the Alpaca
broker client now has a bounded read/connect timeout
(renquant-execution: `AlpacaBroker`'s bounded-timeout session, separate PR).
Without it a single attempt could hang ~82s (the 07:00 case) and 3 attempts
would blow the cadence. The two land together; the umbrella advances both pins
in one cutover.

## Evidence (§4(b))

| claim | value | provenance |
|---|---|---|
| old behaviour: one try, no retry | single `connect()`+`get_account_value()`, exception → HARD False | [VERIFIED — pre-change `_check_broker_connect` / `BrokerConnectTask.check`] |
| runtime path is the Task, not legacy | `build_preflight_pipeline()` → `BrokerConnectTask()` | [VERIFIED — `preflight_pipeline/pipeline.py` line ~94] |
| recovers after N-1 transient fails | fail twice, succeed on 3rd → HARD True, "after 3 attempts", `attempts==3` | [VERIFIED — `test_broker_connect_retry.py`] |
| get_account_value failure also retried | fail once there, succeed → HARD True, "after 2 attempts" | [VERIFIED — same] |
| **fail-closed after exhaustion** | always-fail → **HARD False**, "broker connect failed after 3 attempts", last error surfaced, `attempts==3` | [VERIFIED — `test_fails_closed_after_all_attempts_exhausted` + runtime-task twin] |
| retry loop is BOUNDED | exactly `max_attempts-1` backoffs (2), never more | [VERIFIED — recorded `time.sleep` calls == `[2.0, 2.0]`] |
| None broker still soft-skips | soft PASS "dry-run" | [VERIFIED — `test_none_broker_is_soft_skip`] |
| both entry points share one body | `_attempt_broker_connect` in `BrokerConnectTask.check.__code__.co_names` | [VERIFIED — `test_both_entry_points_route_through_the_shared_body`] |
| new tests load-bearing | error at import against pre-change source (`_attempt_broker_connect` did not exist) | [VERIFIED — `git stash push src/...`, re-run] |
| new tests | **8 passed** | [VERIFIED — `pytest -q tests/test_broker_connect_retry.py`] |
| full pipeline suite | **2552 passed, 11 skipped** (baseline 2544 + 8 new); the 2 pre-existing `test_replay_d6_conventions` failures are numpy/BLAS platform-pin fixture drift on this Darwin/arm64 host, present on unmodified main and unrelated to preflight | [VERIFIED — `pytest -q`, baseline vs. post-change identical failure set] |

artifact: none produced/staged/promoted.
prod or exp: **production preflight gate.** Behaviour change is confined to
  P-BROKER-CONNECT: it now tolerates a bounded number of transient failures
  before the SAME HARD-False refuse-to-trade it always emitted on a genuine
  outage. Fail-closed is preserved; no order-path or sizing behaviour touched.
existing data: yes — the defect and the ~82s hang were read from a live fleet
  log; nothing generated.
best-known?: yes — bounded retry + fast-failing timeout is the minimal honest
  fix; unbounded retry or a longer per-attempt hang would risk the cadence.

## Next

Land alongside the renquant-execution bounded-timeout PR; do not advance this
pin alone. After both merge, the umbrella advances both subrepo pins in one
cutover so the intraday preflight gets fast-failing attempts AND the bounded
retry together.
