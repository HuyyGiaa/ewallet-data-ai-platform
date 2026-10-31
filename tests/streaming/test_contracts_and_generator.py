from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from data_platform.streaming.config.settings import BASELINE, OPTIMIZED
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.contracts.features import FEATURE_OWNERSHIP, Ownership
from data_platform.streaming.contracts.topics import TOPICS, entity_rekey, raw_message_key
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator
from data_platform.streaming.messaging.serializers import deserialize_event, serialize_event


UTC = timezone.utc



def left_sequence(case) -> int:
    for sequence in range(100):
        if case.generator.normal(sequence=sequence, event_time=case.at).transaction_type == "payment":
            return sequence
    raise AssertionError("no payment in first 100 sequences")


class ContractAndGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.generator = DeterministicScenarioGenerator(seed=7001)

    def _payment(self) -> TransactionEvent:
        # The current generator follows offline type weights; pick its first payment.
        return self.generator.normal(sequence=left_sequence(self), event_time=self.at)

    def test_normal_event_is_deterministic_and_round_trips(self) -> None:
        left = self._payment()
        right = DeterministicScenarioGenerator(seed=7001).normal(
            sequence=left_sequence(self), event_time=self.at
        )
        self.assertEqual(left, right)
        self.assertEqual(left, TransactionEvent.from_json(left.to_json()))
        self.assertEqual("1.0", left.schema_version)
        self.assertEqual(left.event_time, left.arrival_time)
        self.assertEqual(left, deserialize_event(serialize_event(left)))
        self.assertIn(left.channel, {"app", "web", "atm"})

    def test_event_domain_matches_canonical_silver_contract(self) -> None:
        event = self._payment()
        invalid_changes = (
            {"transaction_type": "cash"},
            {"status": "reversed"},
            {"channel": "mobile"},
            {"currency": "USD"},
            {"amount": 0.0},
            {"old_balance": -1.0},
            {"new_balance": -1.0},
            {"merchant_id": None},
            {"transaction_type": "transfer", "counterparty_account_id": None},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(event, **changes)

    def test_streaming_boundary_requires_already_normalized_channel(self) -> None:
        event = self._payment()
        for canonical_channel in ("app", "web", "atm", "UNKNOWN"):
            with self.subTest(channel=canonical_channel):
                self.assertEqual(
                    canonical_channel,
                    replace(event, channel=canonical_channel).channel,
                )
        for raw_channel in (" APP ", "APP", "unknown", None):
            with self.subTest(channel=raw_channel), self.assertRaises(ValueError):
                replace(event, channel=raw_channel)

    def test_merchant_and_counterparty_are_conditionally_required(self) -> None:
        payment = self._payment()
        withdrawal = replace(
            payment,
            transaction_type="withdraw",
            merchant_id=None,
            counterparty_account_id=None,
        )
        self.assertIsNone(withdrawal.merchant_id)
        self.assertIsNone(withdrawal.counterparty_account_id)

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
