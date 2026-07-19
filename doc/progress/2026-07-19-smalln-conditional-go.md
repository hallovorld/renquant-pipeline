# Progress: small-n guard conditional-GO amendment

Date: 2026-07-19

## What

doc/design/2026-07-19-smalln-guard-conditional-go.md — amendment to the
eligibility-ledger §4 verdict criterion. The expanded replay over ALL 43
override-era live sessions established only 2 operative-CLEAN small-n days
exist historically (structural max); N_shadow=10 is unmeetable by replay and
impractical by live accrual. But the guard is VERIFIED CORRECT: all 12
operative failure-residue days correctly fail-closed (zero mislabel), the P0's
actual hazard over-proven. Replaces the unmeetable volume bar with an
adverse-day-coverage + bounded-blast-radius + live-detection conditional-GO;
production activation still requires operator on-record authorization.

## Why

The P0 concern was "guard relaxes on failure residue" — proven NEVER to happen
(12/12 adverse days suppressed). Demanding N=10 CLEAN days (structurally 2 max)
leaves a verified-correct fix permanently shadow-dark (deployed-but-dark
anti-pattern). This reframes the evidence standard to what the P0 cares about;
it does NOT self-authorize the flip.

## Status

RFC amendment only. No production key flipped. Operator authorization required
before any activation.
