"""Immutable streaming FeatureView rows for the Feast Offline Store."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ai_platform.features.feast.schema import FEATURE_SPECS
from data_platform.streaming.messaging.consumer_loop import StopSignal
from data_platform.streaming.sinks.offline_features import (
    OfflineFeatureObjectWriter,
    logical_row_id,
    run_offline_feature_writer,
)
from data_platform.streaming.sinks.online import OnlineFeatureUpdate
from data_platform.streaming.verification.consistency import offline_rows_from_feast


UTC = timezone.utc


def update(**overrides) -> OnlineFeatureUpdate:
    spec = FEATURE_SPECS["user_behavior"]
    values = {
        feature: (1 if feature in spec["integer_features"] else 1.5)
        for feature in spec["features"]
    }
    fields = {
        "view_name": "user_behavior",
        "entity_name": "user",
        "entity_key": "user_id",
        "entity_value": "user-1",
        "feature_timestamp": datetime(2026, 11, 11, 1, 2, 3, 456789, tzinfo=UTC),
        "values": values,
        "source_event_id": "event-1",
    }
    return OnlineFeatureUpdate(**{**fields, **overrides})


class Message:
    def __init__(self, payload: bytes, partition: int = 2, offset: int = 9):
        self.payload, self._partition, self._offset = payload, partition, offset

    def value(self):
        return self.payload

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def error(self):
        return None


class Consumer:
    def __init__(self, messages):
        self.messages = list(messages)
        self.commits = []

    def poll(self, _timeout):
        return self.messages.pop(0) if self.messages else None

    def commit(self, message, asynchronous):
        self.commits.append((message.partition(), message.offset(), asynchronous))


class OfflineFeatureWriterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.writer = OfflineFeatureObjectWriter(
            f"file://{self.temporary.name}", "campaign-1", "run-1"
        )

    def test_parquet_has_only_canonical_columns_and_types(self):
        result = self.writer.write(update(), "online-topic", 2, 9)
        table = pq.read_table(result["parquet_path"])
        spec = FEATURE_SPECS["user_behavior"]
        self.assertEqual(
            [spec["entity_key"], "event_timestamp", *spec["features"]],
            table.schema.names,
        )
        self.assertEqual(pa.timestamp("us", tz="UTC"), table.schema.field("event_timestamp").type)
        for feature in spec["integer_features"]:
            self.assertEqual(pa.int64(), table.schema.field(feature).type)
        provenance = json.loads(Path(result["manifest_path"]).read_text())
        self.assertEqual("event-1", provenance["source_event_id"])
        self.assertEqual({"topic": "online-topic", "partition": 2, "offset": 9}, provenance["kafka"])
        self.assertNotIn("source_event_id", table.schema.names)

    def test_identical_replay_reuses_the_same_physical_objects(self):
        first = self.writer.write(update(), "online-topic", 2, 9)
        second = self.writer.write(update(), "online-topic", 2, 9)
        self.assertEqual(first["parquet_uri"], second["parquet_uri"])
        self.assertEqual(first["manifest_uri"], second["manifest_uri"])
        self.assertEqual("REUSED", second["parquet_write"])
        self.assertEqual("REUSED", second["manifest_write"])

    def test_logical_row_identity_is_stable_and_preserves_microseconds(self):
        item = update()
        first = logical_row_id(item)
        self.assertEqual(first, logical_row_id(item))
        self.assertNotEqual(
            first,
            logical_row_id(update(feature_timestamp=item.feature_timestamp.replace(microsecond=456788))),
        )

    def test_same_offset_with_changed_payload_is_a_conflict(self):
        self.writer.write(update(), "online-topic", 2, 9)
        changed = update(source_event_id="event-2")
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            self.writer.write(changed, "online-topic", 2, 9)

    def test_same_logical_row_with_changed_values_is_a_conflict(self):
        self.writer.write(update(), "online-topic", 2, 9)
        values = dict(update().values)
        values["user_tx_count_5m"] = 2
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            self.writer.write(update(values=values), "online-topic", 2, 10)

    def test_naive_timestamp_and_wrong_feature_types_are_refused(self):
        with self.assertRaisesRegex(ValueError, "UTC"):
            self.writer.write(
                update(feature_timestamp=datetime(2026, 11, 11, 1, 2, 3)),
                "online-topic",
                2,
                9,
            )
        values = dict(update().values)
        values["user_tx_count_5m"] = 1.5
        with self.assertRaisesRegex(ValueError, "integer"):
            self.writer.write(update(values=values), "online-topic", 2, 9)

    def test_offset_commits_only_after_both_objects_exist(self):
        message = Message(update().to_json().encode())
        consumer = Consumer([message])
        summary = run_offline_feature_writer(
            consumer, self.writer, StopSignal(), "online-topic", max_idle_seconds=0
        )
        self.assertEqual([(2, 9, False)], consumer.commits)
        self.assertEqual(1, summary["rows_persisted"])

    def test_conflict_is_not_acknowledged(self):
        self.writer.write(update(), "online-topic", 2, 9)
        changed = update(source_event_id="event-2")
        consumer = Consumer([Message(changed.to_json().encode())])
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            run_offline_feature_writer(
                consumer, self.writer, StopSignal(), "online-topic", max_idle_seconds=0
            )
        self.assertEqual([], consumer.commits)

    def test_real_feast_historical_retrieval_reads_persisted_parquet(self):
        from feast import FeatureStore

        timestamps = {}
        for index, (view, spec) in enumerate(FEATURE_SPECS.items()):
            values = {
                feature: (index + 1 if feature in spec["integer_features"] else index + 1.5)
                for feature in spec["features"]
            }
            item = OnlineFeatureUpdate(
                view,
                spec["entity_name"],
                spec["entity_key"],
                f"entity-{index}",
                datetime(2026, 11, 11, 2, index, tzinfo=UTC),
                values,
                f"event-{index}",
            )
            self.writer.write(item, "online-topic", 0, index)
            timestamps[(view, item.entity_value)] = item.feature_timestamp
        offline_root = Path(self.temporary.name) / self.writer.offline_root
        registry = Path(self.temporary.name) / "registry.db"
        environment = {
            "EWALLET_FEAST_REGISTRY": str(registry),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(offline_root),
            "EWALLET_REDIS_CONNECTION_STRING": "127.0.0.1:1,db=15",
        }
        with patch.dict("os.environ", environment):
            from ai_platform.features.feast.online_repo.feature_definitions import (
                build_online_feature_objects,
            )

            store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
            entities, _, pushes, views = build_online_feature_objects(str(offline_root))
            store.apply([*entities.values(), *pushes.values(), *views.values()])
            rows = offline_rows_from_feast(store, timestamps)
        self.assertEqual(set(timestamps), set(rows))
        self.assertEqual(1, rows[("user_behavior", "entity-0")]["user_tx_count_5m"])
        self.assertEqual(timestamps[("merchant_behavior", "entity-3")],
                         rows[("merchant_behavior", "entity-3")]["__ts"])
        self.assertEqual(
            "PIT_VALUES_VERIFIED",
            rows[("merchant_behavior", "entity-3")]["__pit_status"],
        )

    def test_historical_query_time_is_not_used_as_the_source_row_timestamp(self):
        from feast import FeatureStore

        item = update()
        self.writer.write(item, "online-topic", 0, 1)
        offline_root = Path(self.temporary.name) / self.writer.offline_root
        environment = {
            "EWALLET_FEAST_REGISTRY": str(Path(self.temporary.name) / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(offline_root),
            "EWALLET_REDIS_CONNECTION_STRING": "127.0.0.1:1,db=15",
        }
        with patch.dict("os.environ", environment):
            from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
            store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
            entities, _, pushes, views = build_online_feature_objects(str(offline_root))
            store.apply([*entities.values(), *pushes.values(), *views.values()])
            later = item.feature_timestamp.replace(microsecond=456790)
            rows = offline_rows_from_feast(
                store, {(item.view_name, item.entity_value): later}
            )
        row = rows[(item.view_name, item.entity_value)]
        self.assertEqual("PIT_VALUES_VERIFIED", row["__pit_status"])
        self.assertEqual(item.feature_timestamp, row["__ts"])
        self.assertNotEqual(row["__query_ts"], row["__ts"])

    def test_identical_historical_values_do_not_prove_exact_row_identity(self):
        from feast import FeatureStore

        first = update()
        second = update(
            feature_timestamp=first.feature_timestamp.replace(minute=first.feature_timestamp.minute + 5),
            source_event_id="event-2",
        )
        self.writer.write(first, "online-topic", 0, 1)
        self.writer.write(second, "online-topic", 0, 2)
        offline_root = Path(self.temporary.name) / self.writer.offline_root
        environment = {
            "EWALLET_FEAST_REGISTRY": str(Path(self.temporary.name) / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(offline_root),
            "EWALLET_REDIS_CONNECTION_STRING": "127.0.0.1:1,db=15",
        }
        with patch.dict("os.environ", environment):
            from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
            store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
            entities, _, pushes, views = build_online_feature_objects(str(offline_root))
            store.apply([*entities.values(), *pushes.values(), *views.values()])
            rows = offline_rows_from_feast(
                store, {(second.view_name, second.entity_value): second.feature_timestamp}
            )
        row = rows[(second.view_name, second.entity_value)]
        self.assertEqual("AMBIGUOUS_IDENTICAL_VALUES", row["__pit_status"])
        self.assertEqual(second.feature_timestamp, row["__ts"])

    def test_pit_query_between_updates_uses_the_latest_eligible_persisted_row(self):
        from feast import FeatureStore

        first = update()
        later_values = dict(first.values)
        later_values["user_tx_count_5m"] = 2
        second = update(
            feature_timestamp=first.feature_timestamp.replace(minute=first.feature_timestamp.minute + 5),
            values=later_values,
            source_event_id="event-2",
        )
        self.writer.write(first, "online-topic", 0, 1)
        self.writer.write(second, "online-topic", 0, 2)
        offline_root = Path(self.temporary.name) / self.writer.offline_root
        environment = {
            "EWALLET_FEAST_REGISTRY": str(Path(self.temporary.name) / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(offline_root),
            "EWALLET_REDIS_CONNECTION_STRING": "127.0.0.1:1,db=15",
        }
        with patch.dict("os.environ", environment):
            from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
            store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
            entities, _, pushes, views = build_online_feature_objects(str(offline_root))
            store.apply([*entities.values(), *pushes.values(), *views.values()])
            query_time = first.feature_timestamp.replace(minute=first.feature_timestamp.minute + 2)
            rows = offline_rows_from_feast(
                store, {(first.view_name, first.entity_value): query_time}
            )
        row = rows[(first.view_name, first.entity_value)]
        self.assertEqual("PIT_VALUES_VERIFIED", row["__pit_status"])
        self.assertEqual(first.feature_timestamp, row["__ts"])
        self.assertEqual(1, row["user_tx_count_5m"])

    def test_missing_historical_row_is_explicit(self):
        from feast import FeatureStore

        item = update()
        self.writer.write(item, "online-topic", 0, 1)
        offline_root = Path(self.temporary.name) / self.writer.offline_root
        environment = {
            "EWALLET_FEAST_REGISTRY": str(Path(self.temporary.name) / "registry.db"),
            "EWALLET_FEAST_OFFLINE_DATA_ROOT": str(offline_root),
            "EWALLET_REDIS_CONNECTION_STRING": "127.0.0.1:1,db=15",
        }
        with patch.dict("os.environ", environment):
            from ai_platform.features.feast.online_repo.feature_definitions import build_online_feature_objects
            store = FeatureStore(repo_path="ai_platform/features/feast/online_repo")
            entities, _, pushes, views = build_online_feature_objects(str(offline_root))
            store.apply([*entities.values(), *pushes.values(), *views.values()])
            rows = offline_rows_from_feast(
                store, {(item.view_name, "missing-user"): item.feature_timestamp}
            )
        row = rows[(item.view_name, "missing-user")]
        self.assertEqual("MISSING", row["__pit_status"])
        self.assertIsNone(row["__ts"])


if __name__ == "__main__":
    unittest.main()
