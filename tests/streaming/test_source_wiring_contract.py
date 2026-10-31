"""Focused source contracts for the Block A runtime wiring.

Requires PyFlink (the ``flink`` extra of infra/docker/flink-benchmark); without
it the module is skipped and must be reported NOT_EXECUTED, not PASS.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    import pyflink  # noqa: F401
except ImportError:  # pragma: no cover - environment dependent
    raise unittest.SkipTest("NOT_EXECUTED: pyflink not installed (uv extra 'flink')")

from data_platform.streaming.config.settings import OperatingMode, RuntimeSettings, profile_by_name
from data_platform.streaming.flink.operators import (
    BootstrapContractError,
    BootstrapCutoverError,
    ParseAndValidate,
)
from data_platform.streaming.flink.job import _parse_operator
from data_platform.streaming.flink.records import OutboundRecord, RuntimeEvent
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator


UTC = timezone.utc


class _Counter:
    def inc(self) -> None:
        pass


class _MetricGroup:
    def counter(self, name: str) -> _Counter:
        return _Counter()


class _RuntimeContext:
    def get_metric_group(self) -> _MetricGroup:
        return _MetricGroup()


def _opened_parser(**kwargs) -> ParseAndValidate:
    operator = ParseAndValidate(**kwargs)
    operator.open(_RuntimeContext())
    return operator


class SourceWiringContractTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
