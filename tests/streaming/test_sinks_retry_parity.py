from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from data_platform.streaming.contracts.features import Ownership
from data_platform.streaming.feast.online import (
    DEFAULT_ONLINE_FRESHNESS,
    FeastPushWriter,
    MaterializationBoundary,
)
from data_platform.streaming.feast.parity import ParityStatus, compare_feature
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator
from data_platform.streaming.retry.routing import (
    FailureClass,
    route_failure,
    route_invalid_payload,
)
from data_platform.streaming.sinks.historical import HistoricalRecord
from data_platform.streaming.sinks.online import OnlineFeatureUpdate
from ai_platform.features.feast.schema import FEATURE_SPECS


UTC = timezone.utc


class FakeFeatureStore:
    def __init__(self) -> None:
        self.calls = []

    def push(self, source_name, frame, to):
        self.calls.append((source_name, frame, to))


class SinkRetryParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.event = DeterministicScenarioGenerator(7001).normal(1, self.t)

    def test_historical_sink_contract_preserves_provenance(self) -> None:
        record = HistoricalRecord.from_event(self.event, disposition="ON_TIME")
        self.assertEqual(self.event.transaction_id, record.transaction_id)
        self.assertEqual(self.event.event_time, record.event_time)
        self.assertEqual(self.event.arrival_time, record.arrival_time)
        self.assertEqual(self.event.trace_id, record.trace_id)
        self.assertEqual(self.event.schema_version, record.schema_version)

    def test_feast_entity_mapping_and_push_are_schema_safe(self) -> None:
        update = OnlineFeatureUpdate(
            view_name="user_behavior",
            entity_name="user",
            entity_key="user_id",
            entity_value=self.event.user_id,
            feature_timestamp=self.t,
            values={
                feature: (2 if feature == "user_tx_count_5m" else None)
                for feature in FEATURE_SPECS["user_behavior"]["features"]
            },
            source_event_id=self.event.event_id,
        )
        store = FakeFeatureStore()
        writer = FeastPushWriter(
            store,
            frame_factory=lambda rows: rows,
            online_mode="ONLINE",
        )
        writer.write(update)
        source, rows, mode = store.calls[0]
        self.assertEqual("user_behavior_push", source)
        self.assertEqual(self.event.user_id, rows[0]["user_id"])
        self.assertEqual(self.t, rows[0]["event_timestamp"])
        self.assertEqual("ONLINE", mode)
        with self.assertRaisesRegex(ValueError, "complete FeatureView row"):
            OnlineFeatureUpdate(
                view_name="user_behavior",
                entity_name="user",
                entity_key="user_id",
                entity_value=self.event.user_id,
                feature_timestamp=self.t,
                values={"user_tx_count_5m": 2},
                source_event_id=self.event.event_id,
            )

    def test_materialization_is_latest_state_not_history_copy(self) -> None:
        boundary = MaterializationBoundary(
            start=self.t - timedelta(days=30), end=self.t, cutover=self.t
        )
        self.assertEqual(self.t, boundary.end)
        self.assertTrue(boundary.latest_per_entity_only)

    def test_retry_and_dlq_routing(self) -> None:
        retry = route_failure(self.event, "timeout", FailureClass.RETRYABLE, retry_count=1)
        exhausted = route_failure(self.event, "timeout", FailureClass.RETRYABLE, retry_count=3)
        invalid = route_failure(self.event, "bad schema", FailureClass.NON_RETRYABLE, retry_count=0)
        self.assertEqual("retry", retry.destination)
        self.assertEqual("dlq", exhausted.destination)
        self.assertEqual("dlq", invalid.destination)
        self.assertEqual(self.event.trace_id, retry.envelope.trace_id)

        malformed = route_invalid_payload(
            {"transaction_id": "broken", "event_time": "not-a-time"},
            "schema validation failed",
            trace_id="trace-broken",
        )
        self.assertEqual("dlq", malformed.destination)
        self.assertEqual("trace-broken", malformed.envelope.trace_id)
        self.assertEqual("broken", malformed.envelope.original_event["transaction_id"])

    def test_feast_freshness_and_redis_expiry_are_separate_contracts(self) -> None:
        self.assertEqual(timedelta(days=30, minutes=10), DEFAULT_ONLINE_FRESHNESS.feature_view_ttl)
        self.assertEqual(timedelta(minutes=10), DEFAULT_ONLINE_FRESHNESS.max_staleness)
        self.assertIsNone(DEFAULT_ONLINE_FRESHNESS.redis_key_ttl_seconds)
        self.assertFalse(DEFAULT_ONLINE_FRESHNESS.redis_physical_expiry_enabled)

    def test_parity_match_mismatch_stale_missing_and_not_owned(self) -> None:
        self.assertEqual(
            ParityStatus.MATCH,
            compare_feature(2, 2, self.t, self.t, Ownership.HYBRID).status,
        )
        self.assertEqual(
            ParityStatus.MISMATCH,
            compare_feature(2, 3, self.t, self.t, Ownership.HYBRID).status,
        )
        self.assertEqual(
            ParityStatus.MISSING,
            compare_feature(2, None, self.t, None, Ownership.HYBRID).status,
        )
        self.assertEqual(
            ParityStatus.STALE,
            compare_feature(
                2,
                2,
                self.t,
                self.t - timedelta(minutes=20),
                Ownership.HYBRID,
                max_staleness=timedelta(minutes=10),
            ).status,
        )
        self.assertEqual(
            ParityStatus.NOT_REALTIME_OWNED,
            compare_feature(2, None, self.t, None, Ownership.MATERIALIZED_BATCH).status,
        )


if __name__ == "__main__":
    unittest.main()
