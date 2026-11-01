"""Focused source contracts for the Block A runtime wiring.

These tests are intentionally not executed in the source-writing milestone.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from data_platform.streaming.config.settings import OperatingMode, RuntimeSettings, profile_by_name
from data_platform.streaming.flink.records import OutboundRecord


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


if __name__ == "__main__":
    unittest.main()
