"""Focused source contracts for the Block A runtime wiring.

Requires PyFlink (the ``flink`` extra of infra/docker/flink-benchmark); without
it the module is skipped and must be reported NOT_EXECUTED, not PASS.
"""

from __future__ import annotations

import inspect
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pyflink  # noqa: F401
except ImportError:  # pragma: no cover - environment dependent
    raise unittest.SkipTest("NOT_EXECUTED: pyflink not installed (uv extra 'flink')")

from data_platform.streaming.config.settings import OperatingMode, RuntimeSettings, profile_by_name
from data_platform.streaming.flink.operators import (
    BootstrapContractError,
    BootstrapCutoverError,
    EventTimeDisposition,
    ManagedHistoricalFeatures,
    ParseAndValidate,
)
from data_platform.streaming.flink.job import (
    SOURCE_NO_TIMESTAMP,
    SourceEventTimestampAssigner,
    _from_source_with_event_time,
    build_executable_job,
    _from_source_with_watermarks,
    _parse_operator,
    source_topic_names,
    source_event_time_ms,
    uses_source_idleness,
)
from data_platform.streaming.flink.records import EntityEvent, OutboundRecord, RuntimeEvent
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator


UTC = timezone.utc


class _Counter:
    def inc(self) -> None:
        pass


class _MetricGroup:
    def counter(self, name: str) -> _Counter:
        return _Counter()


class _RuntimeContext:
    def get_metrics_group(self) -> _MetricGroup:
        return _MetricGroup()


class _TimerService:
    def current_watermark(self) -> int:
        return 999_999_999

    def register_event_time_timer(self, timestamp: int) -> None:
        pass


class _MapState(dict):
    def put(self, key, value) -> None:
        self[key] = value

    def contains(self, key) -> bool:
        return key in self

    def remove(self, key) -> None:
        del self[key]


class _ValueState:
    def __init__(self) -> None:
        self._value = None

    def value(self):
        return self._value

    def update(self, value) -> None:
        self._value = value


class _KeyedRuntimeContext(_RuntimeContext):
    def get_map_state(self, descriptor) -> _MapState:
        return _MapState()

    def get_state(self, descriptor) -> _ValueState:
        return _ValueState()


class _ProcessContext:
    def timer_service(self) -> _TimerService:
        return _TimerService()


class _RecordingSourceStream:
    def __init__(self) -> None:
        self.uid_value = None
        self.parallelism = None
        self.downstream_watermark_assignments = 0
        self.assigned_strategies = []

    def uid(self, value):
        self.uid_value = value
        return self

    def set_parallelism(self, value):
        self.parallelism = value
        return self

    def assign_timestamps_and_watermarks(self, strategy):
        self.downstream_watermark_assignments += 1
        self.assigned_strategies.append(strategy)
        return self


class _RecordingEnvironment:
    def __init__(self) -> None:
        self.calls = []
        self.stream = _RecordingSourceStream()

    def from_source(self, source, watermark_strategy, name):
        self.calls.append((source, watermark_strategy, name))
        return self.stream


def _opened_parser(**kwargs) -> ParseAndValidate:
    operator = ParseAndValidate(**kwargs)
    operator.open(_RuntimeContext())
    return operator


class SourceWiringContractTest(unittest.TestCase):
    def test_source_timestamp_extracts_every_supported_event_envelope(self) -> None:
        event_time = datetime(2026, 10, 29, 1, 2, 3, 456000, tzinfo=UTC)
        expected_ms = 1_793_235_723_456
        event = DeterministicScenarioGenerator(7001).normal(1, event_time)
        event_payload = event.to_dict()
        cases = {
            "normal": event.to_json(),
            "retry": json.dumps({
                "original_event": event_payload,
                "original_event_time": event_time.isoformat(),
                "retry_count": 1,
                "error_reason": "temporary sink failure",
                "trace_id": event.trace_id,
            }),
            "bootstrap": json.dumps({
                "kind": "BOOTSTRAP",
                "dataset_version": "canonical-100gb-v1",
                "source_run_id": "silver-run-1",
                "cutover": "2026-10-30T00:00:00+00:00",
                "event": event_payload,
            }),
            "benchmark": json.dumps({
                "kind": "BENCHMARK_EVENT",
                "event": event_payload,
                "logical_watermark_ms": expected_ms - 120_000,
            }),
            "benchmark-drain": json.dumps({
                "kind": "BENCHMARK_WATERMARK",
                "event_time_ms": expected_ms + 720_001,
            }),
        }
        expected = {
            "normal": expected_ms,
            "retry": expected_ms,
            "bootstrap": expected_ms,
            "benchmark": expected_ms,
            "benchmark-drain": expected_ms + 720_001,
        }

        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected[name], source_event_time_ms(payload))

    def test_source_timestamp_does_not_advance_for_invalid_or_control_payloads(self) -> None:
        cases = {
            "malformed-json": '{"transaction_id"',
            "invalid-event": '{"transaction_id":""}',
            "bootstrap-complete": json.dumps({
                "kind": "BOOTSTRAP_COMPLETE",
                "dataset_version": "canonical-100gb-v1",
                "source_run_id": "silver-run-1",
                "cutover": "2026-10-30T00:00:00+00:00",
            }),
        }

        for name, payload in cases.items():
            with self.subTest(name=name):
                self.assertEqual(SOURCE_NO_TIMESTAMP, source_event_time_ms(payload))

    def test_source_timestamp_assigner_ignores_broker_timestamp(self) -> None:
        event_time = datetime(2026, 10, 29, 1, 2, 3, 456000, tzinfo=UTC)
        event = DeterministicScenarioGenerator(7001).normal(1, event_time)

        self.assertEqual(
            1_793_235_723_456,
            SourceEventTimestampAssigner().extract_timestamp(event.to_json(), 9_999_999_999_999),
        )

    def test_production_source_assigner_does_not_accept_benchmark_controls(self) -> None:
        marker = json.dumps({"kind": "BENCHMARK_WATERMARK", "event_time_ms": 123_456})

        self.assertEqual(
            SOURCE_NO_TIMESTAMP,
            SourceEventTimestampAssigner(benchmark_replay=False).extract_timestamp(marker, 999),
        )
        self.assertEqual(
            123_456,
            SourceEventTimestampAssigner(benchmark_replay=True).extract_timestamp(marker, 999),
        )

    def test_kafka_source_receives_partition_watermark_strategy(self) -> None:
        environment = _RecordingEnvironment()
        source = object()
        strategy = object()

        stream = _from_source_with_watermarks(
            environment,
            source,
            strategy,
            parallelism=4,
        )

        self.assertEqual(
            [(source, strategy, "redpanda-raw-and-retry")],
            environment.calls,
        )
        self.assertEqual("block-a-redpanda-source-v1", stream.uid_value)
        self.assertEqual(4, stream.parallelism)
        self.assertEqual(0, stream.downstream_watermark_assignments)

    def test_production_event_time_is_applied_after_the_source(self) -> None:
        # from_source drops a Python timestamp assigner, which made watermarks
        # follow broker time; only a downstream assignment honours the payload.
        environment = _RecordingEnvironment()
        source, no_watermarks, strategy = object(), object(), object()

        stream = _from_source_with_event_time(environment, source, no_watermarks, strategy, parallelism=4)

        self.assertEqual([(source, no_watermarks, "redpanda-raw-and-retry")], environment.calls)
        self.assertEqual("block-a-redpanda-source-v1", stream.uid_value)
        self.assertEqual(4, stream.parallelism)
        self.assertEqual([strategy], stream.assigned_strategies)

    def test_only_benchmark_replay_keeps_watermarks_on_the_source(self) -> None:
        body = inspect.getsource(build_executable_job)
        self.assertIn("if settings.mode is OperatingMode.BENCHMARK_REPLAY:", body)
        self.assertIn("_from_source_with_event_time(", body)

    def test_benchmark_source_excludes_undrained_retry_partitions(self) -> None:
        self.assertEqual(
            ("ewallet.transactions.raw.v1.bench-run",),
            source_topic_names(OperatingMode.BENCHMARK_REPLAY, "bench-run"),
        )
        self.assertEqual(
            (
                "ewallet.transactions.raw.v1.live-run",
                "ewallet.transactions.retry.v1.live-run",
            ),
            source_topic_names(OperatingMode.NORMAL_STREAMING, "live-run"),
        )

    def test_outbound_record_preserves_topic_key_and_payload(self) -> None:
        record = OutboundRecord("online", "user:u-1", '{"value":1}')
        self.assertEqual(record, OutboundRecord.from_json(record.to_json()))

    def test_first_deploy_fails_closed_without_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "bootstrap mode missing"):
            RuntimeSettings(
                OperatingMode.FIRST_DEPLOY_BOOTSTRAP,
                profile_by_name("baseline"),
                "redpanda:9092",
                Path("connector.jar"),
                "file:///checkpoints",
            )

    def test_restart_requires_savepoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "savepoint_path"):
            RuntimeSettings(
                OperatingMode.RESTART_RECOVERY,
                profile_by_name("optimized"),
                "redpanda:9092",
                Path("connector.jar"),
                "file:///checkpoints",
            )

    def test_normal_streaming_requires_bootstrap_savepoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "savepoint_path"):
            RuntimeSettings(
                OperatingMode.NORMAL_STREAMING,
                profile_by_name("optimized"),
                "redpanda:9092",
                Path("connector.jar"),
                "file:///checkpoints",
            )

    def test_normal_streaming_accepts_explicit_savepoint(self) -> None:
        settings = RuntimeSettings(
            OperatingMode.NORMAL_STREAMING,
            profile_by_name("optimized"),
            "redpanda:9092",
            Path("connector.jar"),
            "file:///checkpoints",
            savepoint_path="gs://checkpoints/bootstrap-complete",
        )
        self.assertEqual("gs://checkpoints/bootstrap-complete", settings.savepoint_path)

    def test_first_deploy_wires_fail_closed_bootstrap_parser(self) -> None:
        settings = RuntimeSettings(
            OperatingMode.FIRST_DEPLOY_BOOTSTRAP,
            profile_by_name("optimized"),
            "redpanda:9092",
            Path("connector.jar"),
            "gs://checkpoints/block-a",
            silver_root="gs://lakehouse/runs/silver-run-1/silver",
            silver_manifest_uri="gs://evidence/silver-run-1/manifest.json",
            source_run_id="silver-run-1",
            dataset_version="canonical-100gb-v1",
            cutover_iso="2026-10-29T00:00:00+00:00",
        )
        operator = _parse_operator(settings)
        self.assertTrue(operator.bootstrap_only)
        self.assertEqual("canonical-100gb-v1", operator.expected_dataset_version)

    def test_bootstrap_identity_mismatch_fails_job_instead_of_dlq(self) -> None:
        cutover = datetime(2026, 10, 29, tzinfo=UTC).isoformat()
        event = DeterministicScenarioGenerator(7001).normal(
            1, datetime(2026, 10, 28, tzinfo=UTC)
        )
        operator = _opened_parser(
            expected_dataset_version="canonical-100gb-v1",
            expected_source_run_id="silver-run-1",
            expected_cutover=cutover,
            bootstrap_only=True,
        )
        payload = json.dumps(
            {
                "kind": "BOOTSTRAP",
                "dataset_version": "wrong-dataset",
                "source_run_id": "silver-run-1",
                "cutover": cutover,
                "event": event.to_dict(),
            }
        )
        with self.assertRaisesRegex(BootstrapContractError, "dataset_version mismatch"):
            operator.map(payload)

    def test_first_deploy_rejects_live_event_instead_of_advancing_past_it(self) -> None:
        operator = _opened_parser(bootstrap_only=True)
        event = DeterministicScenarioGenerator(7001).normal(
            1, datetime(2026, 10, 29, tzinfo=UTC)
        )
        with self.assertRaisesRegex(BootstrapCutoverError, "live/retry event"):
            operator.map(event.to_json())

    def test_first_deploy_rejects_malformed_unverifiable_payload(self) -> None:
        operator = _opened_parser(bootstrap_only=True)
        with self.assertRaisesRegex(BootstrapContractError, "malformed JSON"):
            operator.map('{"kind":"BOOTSTRAP"')

    def test_normal_streaming_malformed_payload_remains_dlq(self) -> None:
        operator = _opened_parser(bootstrap_only=False)
        output = RuntimeEvent.from_json(operator.map('{"transaction_id"'))
        self.assertEqual("DLQ", output.route)

    def test_normal_invalid_event_remains_an_ordinary_dlq_record(self) -> None:
        operator = _opened_parser(bootstrap_only=False)
        output = RuntimeEvent.from_json(operator.map('{"transaction_id":""}'))
        self.assertEqual("DLQ", output.route)

    def test_benchmark_envelope_is_isolated_from_production_parser(self) -> None:
        event = DeterministicScenarioGenerator(7001).normal(
            1, datetime(2026, 10, 29, tzinfo=UTC)
        )
        payload = json.dumps({
            "kind": "BENCHMARK_EVENT",
            "event": event.to_dict(),
            "logical_watermark_ms": 123,
        })
        production = RuntimeEvent.from_json(_opened_parser().map(payload))
        benchmark = RuntimeEvent.from_json(
            _opened_parser(benchmark_replay=True).map(payload)
        )
        self.assertEqual("DLQ", production.route)
        self.assertEqual("VALID", benchmark.route)
        self.assertEqual(123, benchmark.logical_watermark_ms)

    def test_benchmark_watermark_control_is_not_a_transaction(self) -> None:
        payload = json.dumps({"kind": "BENCHMARK_WATERMARK", "event_time_ms": 456})
        record = RuntimeEvent.from_json(
            _opened_parser(benchmark_replay=True).map(payload)
        )
        self.assertEqual("WATERMARK", record.route)
        self.assertIsNone(record.event)
        self.assertEqual(456, record.control_event_time_ms)

    def test_benchmark_disposition_uses_explicit_watermark_not_scheduler(self) -> None:
        event = DeterministicScenarioGenerator(7001).normal(
            1, datetime(2026, 10, 29, tzinfo=UTC)
        )
        event_ms = int(event.event_time.timestamp() * 1000)
        record = RuntimeEvent(event, "VALID", logical_watermark_ms=event_ms + 700_000)
        operator = EventTimeDisposition(600_000, benchmark_logical_watermark=True)
        operator.open(_RuntimeContext())
        output = RuntimeEvent.from_json(next(operator.process_element(record.to_json(), _ProcessContext())))
        self.assertEqual("TOO_LATE", output.route)
        self.assertEqual(event_ms + 700_000, output.observed_watermark_ms)

    def test_feature_values_do_not_depend_on_state_iteration_order(self) -> None:
        # Naive float sums of these amounts differ by insertion order; the
        # timer must read history in key order so p=1 and p=4 agree bit-for-bit.
        base = DeterministicScenarioGenerator(7001).normal(1, datetime(2026, 10, 29, tzinfo=UTC))
        start = base.event_time
        amounts = [1e16, 1.0, 1.0, 0.1, 0.2]
        history = [
            replace(base, transaction_id=f"tx-{index}", event_id=f"ev-{index}", amount=amount, status="failed",
                    event_time=start + timedelta(seconds=index), arrival_time=start + timedelta(seconds=index))
            for index, amount in enumerate(amounts)
        ]
        current = replace(base, transaction_id="tx-current", event_id="ev-current",
                          event_time=start + timedelta(minutes=1), arrival_time=start + timedelta(minutes=1))

        def run(order):
            operator = ManagedHistoricalFeatures(allowed_lateness_ms=600_000, history_ttl_ms=2_592_600_000)
            operator.open(_KeyedRuntimeContext())
            for event in [history[i] for i in order] + [current]:
                item = EntityEvent("account_behavior", "account_id", event.account_id, event, "ON_TIME", False, True)
                operator.process_element(item.to_json(), _ProcessContext())
            timer = int(current.event_time.timestamp() * 1000) + 600_000 + 1
            return [OutboundRecord.from_json(raw).payload for raw in operator.on_timer(timer, _ProcessContext())]

        forward, backward = run([0, 1, 2, 3, 4]), run([4, 3, 2, 1, 0])
        self.assertEqual(1, len(forward))
        self.assertEqual(forward, backward)

    def test_only_benchmark_replay_disables_source_idleness(self) -> None:
        self.assertFalse(uses_source_idleness(OperatingMode.BENCHMARK_REPLAY))
        for mode in OperatingMode:
            if mode is not OperatingMode.BENCHMARK_REPLAY:
                self.assertTrue(uses_source_idleness(mode), mode)


if __name__ == "__main__":
    unittest.main()
