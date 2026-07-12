# Pipeline sizing-intent contract

**Date:** 2026-07-12
**Scope:** default-off contract implementation for the merged 104/105 cash-drag
experiment protocol.

## Change

Adds a versioned, fail-closed `sizing-intent-v1` record and parser to the pipeline.
The record preserves pipeline-owned pre-quantization target notional, reference
price, planned quantity, cap, reserve, ordinary-buy reservation, and manifest/config
identities. Validation rejects malformed identity, arithmetic, hard-cap, cash, and
outcome fields.

## Boundary

This change does not compute a target in the 105 diagnostic path, enable a strategy
flag, submit an order, or write an orchestrator run bundle. The 105 diagnostic probe
currently has no honest target-notional source; a later producer/wiring PR must add
one before measurement starts. The parser is a pipeline contract for that later
producer and for the 104 paired shadow.

## Evidence and verification

Implements the first pipeline-owned contract requirement from orchestrator PR #490
(`doc/design/2026-07-12-cash-drag-prospective-experiment-protocol.md`). Unit tests
cover valid zero-quantity and floor-rescue records plus fail-closed malformed cases.
