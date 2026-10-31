# Flink Source-Level Partition Watermarks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move event-time watermark generation into `KafkaSource` so active Kafka partitions contribute independent split watermarks before downstream merging.

**Architecture:** A raw-wire timestamp extractor recognizes every supported source envelope and returns a no-timestamp sentinel for invalid/control records. The existing bounded-out-of-orderness strategy is constructed before source creation and passed to `env.from_source`; downstream parsing, deterministic benchmark disposition, deduplication, timers, and sinks remain unchanged.

**Tech Stack:** Python 3.10+, PyFlink 1.19, KafkaSource, `unittest`.

**Spec:** `docs/superpowers/specs/2026-11-01-flink-source-watermarks-design.md`

## Global Constraints

- Modify only Python logic, Python tests, and streaming documentation.
- Do not modify Docker, Compose, Kubernetes, Terraform, GCP, dependency, topic, or resource configuration.
- Preserve benchmark logical watermark isolation and partition-wide drain markers.
- Preserve production idleness and disabled benchmark idleness.
- Do not claim runtime 500 EPS parity until new Docker runs prove identical semantic outputs.

## Review Focus

- Malformed JSON must reach `ParseAndValidate` and must not advance source watermark.
- Retry envelopes must use `original_event.event_time`, not broker/arrival time.
- Benchmark drain markers must advance every source split to their explicit timestamp.
- Bootstrap completion/control records must not move watermark.
- Source watermark assignment must not be followed by a second downstream watermark assignment.

---

### Task 1: Raw Kafka event-time extraction

**Files:**
- Modify: `tests/streaming/test_source_wiring_contract.py`
- Modify: `data_platform/streaming/flink/job.py`

**Interfaces:**
- Produces: `SOURCE_NO_TIMESTAMP: int`, `source_event_time_ms(payload: str | bytes) -> int`, and `SourceEventTimestampAssigner.extract_timestamp(value, record_timestamp) -> int`.
- Consumes: canonical `TransactionEvent`, `FailureEnvelope`, benchmark and bootstrap wire envelopes.

- [ ] **Step 1: Write failing table-driven tests**

Add tests with hand-derived millisecond timestamps for normal events, retry envelopes, bootstrap events, benchmark events, and benchmark drain markers. Add separate malformed JSON, invalid event, and bootstrap-completion tests asserting `SOURCE_NO_TIMESTAMP`.

- [ ] **Step 2: Run tests to verify RED**

Run: `python -m unittest tests.streaming.test_source_wiring_contract.SourceWiringContractTest -v`

Expected: import/error failure because the new extraction API does not exist.

- [ ] **Step 3: Implement the minimal extractor and assigner**

Parse JSON once to select the applicable nested event payload, validate selected events through `TransactionEvent.from_dict`, and convert its timezone-aware `event_time` to epoch milliseconds. Return `SOURCE_NO_TIMESTAMP` for malformed, invalid, or non-event control payloads; preserve explicit benchmark marker `event_time_ms`.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python -m unittest tests.streaming.test_source_wiring_contract.SourceWiringContractTest -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Commit test and implementation as `fix(streaming): extract event time at Kafka source`.

### Task 2: Attach watermark strategy at KafkaSource

**Files:**
- Modify: `tests/streaming/test_source_wiring_contract.py`
- Modify: `data_platform/streaming/flink/job.py`
- Modify: `docs/streaming/flink_local_benchmark_results_20261101.md`

**Interfaces:**
- Consumes: `SourceEventTimestampAssigner` from Task 1.
- Produces: `build_source_watermark_strategy(profile, mode)` and a topology where `env.from_source` owns watermark generation.

- [ ] **Step 1: Write a failing topology contract test**

Exercise the source-construction helper with a recording environment and a sentinel watermark strategy. Assert the exact strategy is supplied to `from_source` and the returned source stream is not passed through a downstream watermark-assignment method.

- [ ] **Step 2: Run the focused test to verify RED**

Run: `python -m unittest tests.streaming.test_source_wiring_contract.SourceWiringContractTest -v`

Expected: FAIL because the source still receives `WatermarkStrategy.no_watermarks()` and downstream assignment remains.

- [ ] **Step 3: Move the strategy to source construction**

Build bounded out-of-orderness plus mode-scoped idleness before `env.from_source`, attach `SourceEventTimestampAssigner`, pass the strategy into the source, and remove downstream `assign_timestamps_and_watermarks`. Keep operator names, UIDs, parallelism, filters, and benchmark logical disposition unchanged.

- [ ] **Step 4: Update the benchmark report without overwriting evidence**

Record the code-level correction as pending runtime verification. Preserve all previous run IDs and retain the 500 EPS failure as historical evidence.

- [ ] **Step 5: Run full verification**

Run the locked container suite:

`docker run --rm -v "$PWD:/workspace:ro" -w /workspace -e PYTHONPATH=/workspace ewallet-flink-bench:local python -m unittest discover -s tests/streaming -t .`

Also run: `git diff --name-only` and confirm no infrastructure path appears.

Expected: all streaming tests PASS; changed paths are limited to the spec, plan, Python logic/test, and streaming report.

- [ ] **Step 6: Commit**

Commit as `fix(streaming): generate watermarks per Kafka split`.
