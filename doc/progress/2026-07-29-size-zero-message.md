# Progress: the skip message blamed cash when cash was never the constraint

STATUS:   delivered. Log message only — the block reason string and every
          sizing decision are byte-for-byte unchanged. Revised after codex
          MED+substantive: the message now keys off INVESTABLE cash (post
          cash-reservation), not raw remaining cash — a large
          `cash_reserve_pct` can leave ample raw cash but far less
          investable, in which case cash genuinely IS the constraint even
          though `remaining_cash >= price`.

WHAT:     `SizeAndEmitTask`'s whole-share `shares < 1` branch now reports which
          quantity actually bound: investable cash below one share (naming
          both the investable and raw figures), or a position target below
          one share with investable cash that was sufficient. `_block(ticker,
          "size_insufficient_cash")` is UNCHANGED.

WHY/DIR:  Measured on the live book 2026-07-27
          [VERIFIED — RenQuant/logs/daily_104/2026-07-27.log:487]:

              SizeAndEmitTask: TSLA insufficient cash — skip
                               (remaining_cash=$9301 price=$309.22)

          There was $9,301 of cash and the share cost $309.22
          [VERIFIED — log:487]. Cash was never the binding constraint. The
          per-name target was ~$231 (2.2% at that conviction)
          [VERIFIED — log:488, AMZN sized "$231, 2.2% target"] and integer
          sizing floors $231/$309 to zero.

          That session placed 2 orders for $463 out of $9,301
          [VERIFIED — log:491, "2 orders placed (spent=$463 /
          starting_cash=$9301)"] while the book sat at ~88% cash
          [DERIVED — $9,301 remaining_cash (log:487) / $10,608.27 equity
          (log:514) = 87.7%, rounded]. Anyone investigating the idle capital
          — I did — is pointed straight at the wrong quantity. A wrong
          explanation is worse than none, because it looks like a funded
          answer and closes the search.

EVIDENCE: artifact: `src/renquant_pipeline/kernel/pipeline/task_selection.py`
                    (whole-share branch), `tests/test_size_zero_message_names_
                    the_real_constraint.py`; live evidence
                    `RenQuant/logs/daily_104/2026-07-27.log`.
  prod or exp:      PROD code, logging only. No sizing arithmetic, no gate, no
                    order path changed.
  existing data:    Yes, measured this session. 07-27 funnel: 118 tickers ->
                    109 candidates [VERIFIED — log:428] -> 80 after the vol
                    gate [VERIFIED — log:429,440; 109-29=80] -> 15 after the
                    weak-buy floor [VERIFIED — log:456; 80-65=15] -> 4 after
                    the conviction gate [VERIFIED — log:457; 15-11=4] ->
                    Kelly sizes 4/4 non-zero at 6.1% avg
                    [VERIFIED — log:459] -> 2 orders, $463 of $9,301
                    [VERIFIED — log:491]. TSLA ($309.22) and EME ($742.73)
                    skipped with the misleading message while $9,301 / $8,838
                    was available [VERIFIED — log:487,490].
  best-known?:      Yes for the message defect. NOT addressed here: why
                    fractional sizing is off for these names — that is a live
                    capital-gate change needing its own design PR
                    (`execution.fractional_shares` is absent from the live
                    config entirely).
  scope:            `renquant-pipeline`, one branch of one task, plus tests.

VERIFICATION:
          `tests/test_size_zero_message_names_the_real_constraint.py`: 4
          passed [VERIFIED — this session]. Two are now BEHAVIORAL — they
          instantiate `SizeAndEmitTask`, run it through `InferenceContext`
          fixtures, and assert on the actual `caplog` output and
          `ctx._blocked_by_ticker`, not on `inspect.getsource()` string
          matches (codex's finding on the prior revision: the tests never
          executed the branch or checked a real logged message). One drives
          the target-bound case (ample investable cash, tiny target — the
          live TSLA shape); the other drives a NEW reserve-limited case
          (`cash_reserve_pct=0.05`, raw cash $600 > price, investable cash
          $100 < price) that reproduces codex's exact counter-example and
          confirms the fixed branch names cash, not the target, when cash
          really is why the order was skipped.
          `tests/test_fractional_sizing_stage2.py` (asserts the reason
          string): 19 passed together with the file above
          [VERIFIED — this session, plain `python3 -m pytest`,
          PYTHONPATH-injected siblings, AND independently re-run with
          `renquant-pipeline/.venv/bin/python3.11` + the same PYTHONPATH].
          Full suite, plain `python3 -m pytest -q tests/`, PYTHONPATH-injected
          siblings, this branch vs `origin/main` @ `10cf32e` in a separate
          worktree: 50 failed / 2067 passed both branch counts (baseline
          2063 passed — delta is exactly the 4 new/changed tests in this
          file); `diff` of the two failing-test-NAME sets is EMPTY — zero
          regressions [VERIFIED — this session]. This 50-failure count does
          NOT match the doc's earlier "2 failures" figure (below), measured
          with the repo's own `.venv`; that venv could not collect this
          worktree's checkout at all (103 collection errors, an editable-
          install/worktree-path artifact, not a code issue) so it was not
          re-usable for a fresh full-suite count this pass — the load-
          bearing fact is the zero-diff against a freshly-fetched `origin/
          main` baseline in a matched environment, not the absolute count,
          which is known to vary by environment (see the file's own
          "Corrections" section below).

NEXT:     Splitting `size_insufficient_cash` into a distinct
          `size_below_one_share` would be more useful still, but it is read by
          tests and possibly by ledger consumers, so it needs its own audit
          rather than riding along with a logging fix.

## Corrections (2026-07-29, rq-fix pass)

Two numbers in the original version of this doc (and the mirrored PR body)
turned out wrong once re-measured to add the per-number provenance tags that
long-term agreement #10 requires. Recorded visibly, not silently overwritten:

1. **"50% cash" -> ~88% cash.** $9,301 remaining cash against $10,608.27
   total equity at the time (`log:487,514`) is 87.7% cash, not 50%. The
   corrected number makes the underlying point (idle capital) stronger, not
   weaker — it does not change the conclusion.
2. **"51 failures both" -> 2 failures both.** Re-run this session with the
   repo's actual `.venv` shows 2 pre-existing failures at both `HEAD` and
   `HEAD~1` (== `origin/main`), unrelated to this change; the original count
   did not reproduce in this environment and the cause was not chased
   further since it doesn't change the conclusion. "ZERO introduced" still
   holds — only the count backing it does not.
