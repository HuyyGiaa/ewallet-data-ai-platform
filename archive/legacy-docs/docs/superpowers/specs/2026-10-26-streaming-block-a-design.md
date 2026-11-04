# Streaming Block A design

## Intent

Build the canonical data-serving path from deterministic transaction events
through Redpanda and Flink to immutable history plus Feast Online backed by
Redis. Preserve the verified F5/F6 feature semantics; do not implement model
inference, KServe, KFP, MLflow, or training.

## Constraints

- DP1, DP2, DP3 and the 41-column model contract remain unchanged.
- The 31 historical features, entity keys, UTC timestamps, null behavior and
  `[T-window,T)` cutoff are imported from the existing schema contract.
- No cloud resources or workloads are changed during implementation.
- Redis stores only the latest Feast value for an entity; replayable events
  and immutable history remain outside Redis.

## Approaches considered

1. Extend the legacy PyFlink demo in place. Rejected because it combines
   parsing, connectors, state, windows, sinks and CLI constants, and implements
   a tumbling window that does not match the rolling F5 contract.
2. Write directly to raw Redis keys from Flink. Rejected because this bypasses
   Feast entity serialization, timestamps, TTL and retrieval semantics.
3. Add a modular canonical package and retain legacy code as a temporary
   reference. Selected because it isolates contracts from adapters, supports
   deterministic dependency-light tests, and provides an explicit Feast push
   boundary without deleting historical coursework.

## Canonical flow

`generator -> transactions.raw.v1 -> Flink contract processor`

The processor validates schema, deduplicates by `transaction_id`, classifies
event time, persists every valid unique event to `transactions.accepted.v1`,
and fans accepted online events into user/account/device/merchant keyed state.
Feature snapshots are computed before the current timestamp group is inserted.
Updates are emitted to `features.online.v1`, where a separate writer calls
Feast `FeatureStore.push`; it never writes raw Redis keys.

Retryable transport/sink failures go to `transactions.retry.v1`. Invalid,
non-retryable, exhausted and poison messages go to `transactions.dlq.v1`.
Valid too-late events enter both immutable history and
`transactions.late.v1` for reconciliation; they never update online state.

## Feature ownership and bootstrap

All 31 features are `HYBRID`, not pure realtime. Every feature requires either
prior event history or the device first-seen dimension. DP3 materialization
loads the latest complete rows into Redis at cutover `T0`. For first deployment,
Flink then replays immutable run-scoped Silver transactions over
`[T0-30d,T0)`, suppresses online writes, loads the pre-window account anchors
and Silver device dimension, validates offline/online parity, and only then
begins updates for events at or after `T0`. The accepted topic is not assumed
to predate deployment. Normal operation consumes the raw topic; restarts use a
checkpoint/savepoint plus retained raw records. A latest Redis aggregate is
never treated as enough to reconstruct rolling state.

## Event-time rules

Event time is the business timestamp; arrival time is broker/generator time.
The default watermark is maximum observed event time minus two minutes, with
ten minutes allowed lateness and 30 seconds idleness. On-time and accepted-late
events may update state. Too-late events are retained in history and routed to
the dedicated late/reconciliation topic, never silently dropped or classified
as poison. Same-entity events sharing an
event timestamp receive the same pre-event snapshot.

## State and delivery

Dedup state lasts 48 hours, covering the configured 24-hour late-event range,
retries and margin. Rolling feature state retains 30 days plus allowed
lateness. Feast FeatureView TTL and the 10-minute serving freshness check are
semantic controls. Feast 0.66 Redis physical expiry is separately disabled
with `key_ttl_seconds: null`; Flink state TTL remains 30d+10m. Redpanda raw
records are keyed by transaction ID; Flink performs the
minimum internal fan-out/rekey for four entity states. Online update records
are keyed by `entity_type:entity_id`.

Baseline and optimized profiles have identical semantic settings. The
optimized profile changes parallelism, incremental state/checkpoint behavior
and sink batching only. Later evidence must report throughput, latency,
freshness, lag, backpressure, lateness, dedup, retry/DLQ, checkpoint/state and
offline/online parity.
