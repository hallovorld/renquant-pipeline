# P-BROKER-CONNECT bounded retry, fail-closed preserved   (PR #286)

STATUS: delivered.

WHAT: adds a bounded retry to the P-BROKER-CONNECT preflight so a single
transient Alpaca network blip no longer aborts the whole intraday cycle. New
shared body `_attempt_broker_connect(broker, *, max_attempts=3,
backoff_seconds=2.0)` in `kernel/preflight.py` retries `connect()` +
`get_account_value()` up to 3× with a ~2s backoff; HARD True on the first
success, HARD False only after ALL attempts fail (fail-closed preserved). Both
the runtime `BrokerConnectTask` (the live path, registered by
`build_preflight_pipeline()`) and the legacy `_check_broker_connect` bridge
route through that one body, closing the twin-task drift risk. `import time`
added; the None/dry-run soft-skip is unchanged.

WHY/DIR: closes the P-BROKER-CONNECT single-blip abort (2026-08-11 07:01
intraday cycle lost, no orders for ~12 min). Pairs with the renquant-execution
bounded read/connect timeout PR (#41): the retry is only SAFE to add because
each account read is now bounded, so the worst-case preflight budget is finite
and well under cadence (see EVIDENCE latency bound). The two land together; the
umbrella advances both subrepo pins in one cutover.

EVIDENCE:

The bug (verified 2026-08-11) — the `intraday_104` 07:01 run aborted:

```
✗ P-BROKER-CONNECT [HARD] broker connect failed:
  HTTPSConnectionPool(host='api.alpaca.markets', port=443):
  Read timed out. (read timeout=None)
```

The check did ONE `broker.connect()` + `broker.get_account_value()`; any
exception → HARD False, no retry. One transient blip aborted the cycle (no
orders), recovering only on the next scheduled run ~12 min later.

Latency / cadence bound — auditable, per the actual call path.

Exact per-attempt account-read count (read from `_attempt_broker_connect`,
`kernel/preflight.py:1373-1385`): one retry attempt runs

```
broker.connect()            # renquant-execution: 1 account read (get_account())
eq = broker.get_account_value()   # 1 account read (_refresh_account() -> get_account())
```

so **2 account reads per attempt** (both the runtime `BrokerConnectTask.check`
and the legacy `_check_broker_connect` call this same body — verified, they
route through it). `connect()` raising short-circuits `get_account_value()`, so
2 is the per-attempt maximum.

Timeout assumptions (from the paired renquant-execution #41 change): each
account read carries a bounded `requests` timeout `(connect=5s, read=10s)`.
Those are sequential phase caps, so a single read's worst-case wall time before
it raises is `5 + 10 = 15s`.

Guaranteed worst-case preflight budget (DERIVED). The max-time failing attempt
is `connect()` succeeding slowly (up to 15s, no raise) then `get_account_value()`
timing out (up to 15s, raises): `2 × 15s = 30s`. To fail closed the loop runs
all `max_attempts = 3` attempts with `max_attempts − 1 = 2` backoffs of 2s
(backoff only occurs between attempts, only after a failure):

```
worst_case = attempts × reads_per_attempt × per_read_cap  +  (attempts − 1) × backoff
           = 3        × 2                 × 15s            +  2              × 2s
           = 90s + 4s = 94s
```

**≈ 94s guaranteed worst case ≪ the ~12-min (720s) intraday cadence.** This is a
DERIVED upper bound, not an observation.

Distinguish from the single observed incident (EMPIRICAL, n = 1): the
2026-08-11 07:00 event was ONE unbounded read that hung ~82s (07:00:10 →
07:01:32) and then HARD-aborted the cycle with NO retry. That ~82s is one
measured instance of the OS-level TCP timeout for that specific stall, not a
cap the code guaranteed — an unbounded `timeout=None` read has no firm upper
bound at all.

Why the two must land together (the "without it" claim, restated honestly):
without #41's bounded timeout, each read is `timeout=None` → unbounded, so a
3-attempt × 2-read retry has NO guaranteed upper bound. Even taking the observed
~82s as if it were a per-read cap gives `3 × 2 × 82 + 2 × 2 ≈ 496s` — already
two-thirds of the 720s cadence and still not a real ceiling, since a stalled
socket can exceed the observed 82s. Adding retries on top of unbounded reads is
therefore unsafe; it is #41's bounded `(5s, 10s)` timeout that makes the
worst case finite (~94s) and provably under cadence. Do not raise
`max_attempts` / `backoff` beyond what `attempts × 2 × (connect+read) +
(attempts−1) × backoff` keeps under cadence.

| claim | value | provenance |
|---|---|---|
| old behaviour: one try, no retry | single `connect()`+`get_account_value()`, exception → HARD False | [VERIFIED — pre-change `_check_broker_connect` / `BrokerConnectTask.check`] |
| runtime path is the Task, not legacy | `build_preflight_pipeline()` → `BrokerConnectTask()` | [VERIFIED — `preflight_pipeline/pipeline.py`] |
| reads per attempt | 2 (`connect()` → 1, `get_account_value()` → 1) | [VERIFIED — `kernel/preflight.py:1373-1385`; `renquant-execution` `connect()`/`get_account_value()`] |
| per-read worst-case | `(connect 5s + read 10s) = 15s` | [VERIFIED — paired #41 timeout `(5.0, 10.0)`] |
| guaranteed worst-case budget | `3×2×15 + 2×2 = 94s` ≪ 720s cadence | [DERIVED — from the two rows above] |
| observed incident (distinct) | one ~82s unbounded hang, n=1, aborted with NO retry | [VERIFIED — intraday_104 log 2026-08-11 07:00:10→07:01:32] |
| recovers after N-1 transient fails | fail twice, succeed on 3rd → HARD True, "after 3 attempts", `attempts==3` | [VERIFIED — `test_broker_connect_retry.py`] |
| get_account_value failure also retried | fail once there, succeed → HARD True, "after 2 attempts" | [VERIFIED — same] |
| **fail-closed after exhaustion** | always-fail → **HARD False**, "broker connect failed after 3 attempts", last error surfaced, `attempts==3` | [VERIFIED — `test_fails_closed_after_all_attempts_exhausted` + runtime-task twin] |
| retry loop is BOUNDED | exactly `max_attempts-1` backoffs (2), never more | [VERIFIED — recorded `time.sleep` calls == `[2.0, 2.0]`] |
| None broker still soft-skips | soft PASS "dry-run" | [VERIFIED — `test_none_broker_is_soft_skip`] |
| both entry points share one body | `_attempt_broker_connect` in `BrokerConnectTask.check.__code__.co_names` | [VERIFIED — `test_both_entry_points_route_through_the_shared_body`] |
| new tests load-bearing | error at import against pre-change source (`_attempt_broker_connect` did not exist) | [VERIFIED — `git stash push src/...`, re-run] |
| new tests | **8 passed** | [VERIFIED — `pytest -q tests/test_broker_connect_retry.py`] |
| full pipeline suite | **2607 passed, 7 skipped, 0 failed** (independent re-run). The `test_replay_d6_conventions` numpy/BLAS platform-drift failures seen on the author's Darwin/arm64 host do NOT occur in a clean-BLAS env, confirming they are environmental and unrelated to preflight | [VERIFIED — `pytest -q` on the RenQuant venv] |

artifact:      src/renquant_pipeline/kernel/preflight.py (`_attempt_broker_connect`); src/renquant_pipeline/kernel/preflight_pipeline/tasks/broker.py (`BrokerConnectTask`); tests/test_broker_connect_retry.py
prod or exp:   prod (production preflight gate). Behaviour change confined to P-BROKER-CONNECT: it now tolerates a bounded number of transient failures before the SAME HARD-False refuse-to-trade it always emitted on a genuine outage. Fail-closed preserved; no order-path or sizing behaviour touched.
existing data: yes — the defect and the ~82s hang were read from a live fleet log (`intraday_104` 2026-08-11 07:00→07:01); nothing generated.
best-known?:   yes — bounded retry + fast-failing timeout is the minimal honest fix; unbounded retry or a longer per-attempt hang would risk the cadence. The shared-body design (one `_attempt_broker_connect` for both entry points) is strictly better than duplicating retry logic across the twin task implementations.
scope:         "this is the P-BROKER-CONNECT preflight check (prod), vs existing best = today's single-try, no-retry gate that HARD-aborts the cycle on one transient blip; the bounded retry (3 attempts, 2 reads/attempt, ~94s guaranteed worst case with #41's (5s,10s) read timeout) recovers from transient blips while preserving the identical fail-closed HARD-False on a genuine outage"

NEXT: land alongside the renquant-execution bounded-timeout PR (#41); do not
advance this pin alone. After both merge, the umbrella advances both subrepo
pins in one cutover so the intraday preflight gets fast-failing attempts AND the
bounded retry together.
