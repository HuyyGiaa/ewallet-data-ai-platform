from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from data_platform.streaming.config.settings import BASELINE, OPTIMIZED
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.features import FEATURE_OWNERSHIP, Ownership
from data_platform.streaming.contracts.topics import TOPICS, entity_rekey, raw_message_key
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator
from data_platform.streaming.messaging.serializers import deserialize_event, serialize_event


UTC = timezone.utc


class ContractAndGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.generator = DeterministicScenarioGenerator(seed=7001)

    def test_normal_event_is_deterministic_and_round_trips(self) -> None:
        left = self.generator.normal(sequence=7, event_time=self.at)
        right = DeterministicScenarioGenerator(seed=7001).normal(
            sequence=7, event_time=self.at
        )
        self.assertEqual(left, right)
        self.assertEqual(left, TransactionEvent.from_json(left.to_json()))
        self.assertEqual("1.0", left.schema_version)
        self.assertEqual(left.event_time, left.arrival_time)
        self.assertEqual(left, deserialize_event(serialize_event(left)))

    def test_duplicate_and_disorder_scenarios_preserve_identity(self) -> None:
        event = self.generator.normal(sequence=1, event_time=self.at)
        duplicate = self.generator.duplicate(event, arrival_delay=timedelta(minutes=2))
        out_of_order = self.generator.out_of_order(
            sequence=2,
            arrival_time=self.at,
            event_delay=timedelta(minutes=3),
        )
        late = self.generator.late(
            sequence=3,
            arrival_time=self.at,
            event_delay=timedelta(minutes=20),
        )
        self.assertEqual(event.transaction_id, duplicate.transaction_id)
        self.assertEqual(event.event_id, duplicate.event_id)
        self.assertGreater(duplicate.arrival_time, event.arrival_time)
        self.assertLess(out_of_order.event_time, out_of_order.arrival_time)
        self.assertLess(late.event_time, out_of_order.event_time)

    def test_invalid_fixture_fails_canonical_schema(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            TransactionEvent.from_dict(self.generator.invalid_payload(sequence=9))

    def test_topics_and_rekey_contract(self) -> None:
        event = self.generator.normal(sequence=1, event_time=self.at)
        self.assertEqual(event.transaction_id, raw_message_key(event))
        keys = entity_rekey(event)
        self.assertEqual(f"user:{event.user_id}", keys["user_behavior"])
        self.assertEqual(f"account:{event.account_id}", keys["account_behavior"])
        self.assertEqual(f"device:{event.device_id}", keys["device_behavior"])
        self.assertIn("raw", TOPICS)
        self.assertEqual(12, TOPICS["raw"].partitions)
        self.assertEqual("ewallet.transactions.late.v1", TOPICS["late"].name)
        self.assertNotEqual(TOPICS["late"].name, TOPICS["dlq"].name)

    def test_all_31_features_are_explicitly_owned(self) -> None:
        self.assertEqual(31, len(FEATURE_OWNERSHIP))
        self.assertEqual({Ownership.HYBRID}, {item.ownership for item in FEATURE_OWNERSHIP})
        self.assertTrue(all(item.bootstrap_requirement for item in FEATURE_OWNERSHIP))

    def test_baseline_and_optimized_keep_semantics_equal(self) -> None:
        self.assertEqual(BASELINE.event_time, OPTIMIZED.event_time)
        self.assertEqual(BASELINE.dedup_ttl, OPTIMIZED.dedup_ttl)
        self.assertEqual(1, BASELINE.parallelism)
        self.assertEqual(4, OPTIMIZED.parallelism)


if __name__ == "__main__":
    unittest.main()
