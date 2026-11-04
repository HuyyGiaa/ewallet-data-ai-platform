# Flink Source-Level Partition Watermarks Design

## Intent

Make production and performance-replay event-time progress partition-aware so a fast Kafka partition cannot finalize feature timers while an active slower partition still has earlier records queued. Preserve the existing deterministic functional benchmark, genuine late/too-late outcomes, and real Flink event-time timers.

## Goals

- Attach the bounded-out-of-orderness watermark strategy directly to `KafkaSource` via `env.from_source(...)`.
- Extract timestamps from every supported raw wire envelope: normal transaction, retry, bootstrap, benchmark event, and benchmark drain watermark.
- Ensure malformed, completion, or otherwise non-event records cannot advance a source watermark; they must continue to reach the existing parser/DLQ or control path.
- Keep production idleness enabled and benchmark idleness disabled, as today.
- Keep `EventTimeDisposition` logical-watermark classification isolated to `BENCHMARK_REPLAY`; production continues to classify against the Flink runtime watermark.
- Add regression tests that fail while the source still uses `no_watermarks()` followed by downstream watermark assignment.

## Non-goals

- No Dockerfile, Compose, image, dependency, Kubernetes, GCP, topic, or resource-budget changes.
- No change to checkpoint profiles, Kafka sink delivery guarantees, feature formulas, dedup TTL, allowed lateness, or benchmark payloads.
- No claim that 500 EPS parity or throughput is fixed until the user reruns the real Docker benchmark and the evidence comparator passes.
- No end-to-end exactly-once sink or feature-operator performance refactor in this change.

## Constraints and Safety

- Invalid JSON must not fail timestamp extraction or move event time forward. It receives a no-timestamp sentinel and remains available to `ParseAndValidate` for DLQ routing.
- A benchmark drain marker must retain its explicit `event_time_ms` so every source split can advance and fire pending timers.
- Source timestamp parsing and normal event parsing must use the same transaction/retry contracts to avoid divergent interpretations.
- Existing production modes retain the configured idle timeout; benchmark replay relies on one drain marker per raw partition and must not mark a backpressured split idle.

## Approaches Considered

1. **KafkaSource watermark strategy (selected).** Add a raw-payload timestamp assigner and pass the configured strategy into `from_source`. Flink then maintains split watermarks and combines active inputs safely. This is the smallest supported change and addresses the suspected root cause.
2. **Custom downstream partition coordinator.** Rejected because the value-only source does not retain partition metadata and reimplementing Flink watermark coordination would add unnecessary state and failure modes.
3. **Benchmark-only logical watermarks.** Rejected as the sole fix because it already makes functional replay deterministic but leaves production/performance runtime timers scheduler-dependent under backlog.

## Code Shape

- `data_platform/streaming/flink/job.py`
  - Add a dependency-light raw Kafka timestamp extraction function/assigner.
  - Build the watermark strategy before source creation.
  - Pass that strategy to `env.from_source(...)`.
  - Remove downstream `assign_timestamps_and_watermarks(...)` while retaining parse/filter topology.
- `tests/streaming/test_source_wiring_contract.py`
  - Cover timestamps for normal, retry, bootstrap, benchmark event, and drain marker payloads.
  - Cover malformed/control payloads not advancing watermarks.
  - Lock the topology contract: watermark strategy belongs to `from_source`, and the parsed stream does not assign watermarks again.
- Streaming benchmark documentation may be updated only to describe the code change and explicitly require new runtime evidence; existing evidence and run IDs remain untouched.

## Acceptance Criteria

- New tests demonstrate the old topology is rejected and the new source-level topology is required.
- All locked streaming tests pass in the existing benchmark image/environment.
- No infrastructure files change.
- Functional baseline/optimized must be rerun by the user and produce identical finalized semantic hashes before performance results are considered.
- A 500 EPS run is considered corrected only if repeated runs are stable and baseline/optimized online feature outputs are identical, not merely equal in count.
