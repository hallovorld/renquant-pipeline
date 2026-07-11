# run_governor_sizing §1091 no-sell guard: scope to asset class

Date: 2026-07-10
PR: fix(crypto): scope run_governor_sizing's §1091 no-sell guard to asset class (#184)

## What

Follow-up to #183 (merged at `0115a05`): #183 fixed three call sites that
invoked the wash-sale gate helpers with no `asset_class`/
`validated_crypto_pairs` kwargs (Codex review, commit 7aa82cf5). While
independently verifying that fix, found a fourth, closely related gap in
the **same function** (`run_governor_sizing`) that Codex's review did not
name: an inline §1091 no-sell guard — floors a held ticker's weight
(blocks selling) if it's a loss lot bought inside the wash-sale window —
that never checked asset class at all.

A validated crypto spot pair (`asset_class="crypto"`, in the operator's
`crypto_spot_pairs` allowlist) held at a loss inside the window was
therefore wrongly floored, even though IRC §1091 does not apply to crypto
(property, not a security) — the exact P5 correctness property #183
exists to establish, just missed on this function's sell side.

## Fix

Gate the guard behind `wash_sale_applies_for_ticker(asset_class, ticker,
validated_crypto_pairs)` (the same P5 primitive #183 introduced). Fail
closed unchanged: an `asset_class="crypto"`-tagged but unvalidated ticker,
and any equity ticker, are still floored.

## Tests

`TestP5WashSaleBypass::test_governor_sizing_no_sell_1091_guard_bypassed_for_validated_crypto_only`
spies on `allocate_down_only`'s `no_sell` kwarg (the function's own
downstream consumer of the guard) to prove the three-way split: validated
crypto bypasses, unvalidated crypto still blocked, equity unaffected.
Confirmed meaningful via stash-revert against pre-fix code.

Full suite: 1536 passed / 8 skipped / 5 failed — all 5 failures reproduced
identically on the pre-fix commit in the same environment (xgboost/
replay-pin artifacts), confirmed unrelated to this change.
