# Progress: the skip message blamed cash when cash was never the constraint

STATUS:   delivered. Log message only — the block reason string and every
          sizing decision are byte-for-byte unchanged.

WHAT:     `SizeAndEmitTask`'s whole-share `shares < 1` branch now reports which
          quantity actually bound: cash below one share, or a position target
          below one share with ample cash. `_block(ticker,
          "size_insufficient_cash")` is UNCHANGED.

WHY/DIR:  Measured on the live book 2026-07-27:

              SizeAndEmitTask: TSLA insufficient cash — skip
                               (remaining_cash=$9301 price=$309.22)

          There was $9,301 of cash and the share cost $309.22. Cash was never
          the binding constraint. The per-name target was ~$231 (2.2% at that
          conviction) and integer sizing floors $231/$309 to zero.

          That session placed 2 orders for $463 out of $9,301 while the book
          sat at 50% cash. Anyone investigating the idle capital — I did — is
          pointed straight at the wrong quantity. A wrong explanation is worse
          than none, because it looks like a funded answer and closes the
          search.

EVIDENCE: artifact: `src/renquant_pipeline/kernel/pipeline/task_selection.py`
                    (whole-share branch), `tests/test_size_zero_message_names_
                    the_real_constraint.py`; live evidence
                    `RenQuant/logs/daily_104/2026-07-27.log`.
  prod or exp:      PROD code, logging only. No sizing arithmetic, no gate, no
                    order path changed.
  existing data:    Yes, measured this session. 07-27 funnel: 118 tickers ->
                    109 candidates -> 80 after the vol gate -> 15 after the
                    weak-buy floor -> 4 after the conviction gate -> Kelly
                    sizes 4/4 non-zero at 6.1% avg -> 2 orders, $463 of
                    $9,301. TSLA ($309.22) and EME ($742.73) skipped with the
                    misleading message while $9,301 / $8,838 was available.
  best-known?:      Yes for the message defect. NOT addressed here: why
                    fractional sizing is off for these names — that is a live
                    capital-gate change needing its own design PR
                    (`execution.fractional_shares` is absent from the live
                    config entirely).
  scope:            `renquant-pipeline`, one branch of one task, plus tests.

VERIFICATION:
          Full suite, same worktree, change stashed vs applied: 51 failures
          both, ZERO introduced. `tests/test_fractional_sizing_stage2.py`
          (which asserts the reason string) 15 passed. 3 new tests, one of
          which fails if the reason string is ever changed without an audit.

NEXT:     Splitting `size_insufficient_cash` into a distinct
          `size_below_one_share` would be more useful still, but it is read by
          tests and possibly by ledger consumers, so it needs its own audit
          rather than riding along with a logging fix.
