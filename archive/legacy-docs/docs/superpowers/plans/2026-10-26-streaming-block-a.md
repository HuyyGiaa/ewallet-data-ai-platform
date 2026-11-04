# Streaming Block A implementation plan

**Goal:** Prepare a modular, tested Block A implementation and runbook without
deploying infrastructure or changing verified batch/ML contracts.

### Task 1: Canonical contracts and deterministic generator

**Files:** `data_platform/streaming/contracts/*`,
`data_platform/streaming/generator/*`, `tests/streaming/test_contracts.py`

- [x] Write failing tests for schema validation, serialization, deterministic
  normal/duplicate/out-of-order/late/invalid scenarios and topic keys.
- [x] Implement immutable event, topic and feature ownership contracts.
- [x] Verify focused tests.

### Task 2: Event-time, dedup, feature state and retry behavior

**Files:** `data_platform/streaming/flink/*`,
`data_platform/streaming/retry/*`, `tests/streaming/test_processing.py`

- [x] Write failing tests for watermark classes, TTL expiry, retry/DLQ,
  `[T-window,T)`, same-time peers, cold start and four-entity rekeying.
- [x] Implement dependency-light canonical operators and topology contract.
- [x] Verify focused tests.

### Task 3: Historical and Feast Online boundaries

**Files:** `data_platform/streaming/sinks/*`,
`data_platform/streaming/feast/*`, online Feast repo files,
`tests/streaming/test_sinks_and_parity.py`

- [x] Write failing tests for immutable history records, entity mapping,
  materialization cutover, push updates, stale/missing/parity outcomes.
- [x] Implement Feast-supported push adapter and no-Redis-key abstraction.
- [x] Verify focused tests.

### Task 4: Configuration, documentation and verification

**Files:** `configs/streaming/*`, `docs/streaming/block_a.md`, requirements.

- [x] Add baseline/optimized profiles with identical semantic settings.
- [x] Document legacy classification, all 31 ownership decisions, bootstrap,
  deployment/rollback and evidence plan.
- [x] Run focused suite, compile, configuration parse, contract regressions and
  `git diff --check`; inspect staged diff and commit once.

## Review focus

- UTC-naive timestamps must be rejected rather than interpreted locally.
- Same-time peers must share a snapshot and be inserted only after emission.
- Too-late valid events must reach history even when excluded from online state.
- Retry envelopes must retain original payload, trace ID and attempt count.
- Feature ownership must exactly cover the existing 31 names without extras.
