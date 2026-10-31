from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.generator.scenarios import (
    DeterministicScenarioGenerator,
    GeneratorProfile,
)


UTC = timezone.utc


class GeneratorCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        self.generator = DeterministicScenarioGenerator(seed=42)

    def test_current_event_is_deterministic_and_flink_parseable(self) -> None:
        left = self.generator.normal(7, self.start)
        right = DeterministicScenarioGenerator(seed=42).normal(7, self.start)
        self.assertEqual(left, right)
        self.assertEqual(left, TransactionEvent.from_json(left.to_json()))
        self.assertEqual("1.0", left.schema_version)

    def test_offline_v2_shape_and_required_types(self) -> None:
        event = self.generator.normal(9, self.start)
        record = event.to_offline_record()
        self.assertEqual(
            {
                "transaction_id", "account_id", "user_id", "device_id", "type",
                "amount", "currency", "status", "channel", "old_balance",
                "new_balance", "merchant_id", "counterparty_account_id",
                "timestamp", "ingested_at",
            },
            set(record),
        )
        self.assertIsInstance(record["amount"], float)
        self.assertIsInstance(record["timestamp"], datetime)
        self.assertNotIn("is_fraud", record)
        self.assertNotIn("fraud_type", record)

    def test_all_generated_references_are_valid(self) -> None:
        for sequence in range(100):
            event = self.generator.normal(sequence, self.start)
            self.generator.catalog.validate(event)

    def test_fraud_ground_truth_is_not_emitted(self) -> None:
        payload = self.generator.normal(1, self.start).to_dict()
        forbidden = {"is_fraud", "fraud_type", "label", "scenario"}
        self.assertFalse(forbidden.intersection(payload))

    def test_duplicate_preserves_dedup_identity(self) -> None:
        event = self.generator.normal(2, self.start)
        duplicate = self.generator.duplicate(event, timedelta(seconds=2))
        self.assertEqual(event.event_id, duplicate.event_id)
        self.assertEqual(event.transaction_id, duplicate.transaction_id)
        self.assertGreater(duplicate.arrival_time, event.arrival_time)

    def test_late_arrival_has_explicit_event_and_ingestion_semantics(self) -> None:
        event = self.generator.late(3, self.start, timedelta(minutes=30))
        self.assertEqual(timedelta(minutes=30), event.arrival_time - event.event_time)
        offline = event.to_offline_record()
        self.assertEqual(event.event_time, offline["timestamp"])
        self.assertEqual(event.arrival_time, offline["ingested_at"])

    def test_profile_retains_target_rates_and_separates_stress_rate(self) -> None:
        profile = GeneratorProfile()
        self.assertEqual(42, profile.seed)
        self.assertEqual(0.12, profile.late_arrival_rate)
        self.assertEqual(0.015, profile.duplicate_rate)
        self.assertEqual(30, profile.burst_multiplier)
        self.assertEqual(
            profile.realistic_events_per_second * 30,
            profile.emission_rate(stress=True),
        )
        self.assertEqual(
            profile.realistic_events_per_second,
            profile.emission_rate(stress=False),
        )

    def test_bounded_sample_applies_exact_late_and_duplicate_targets(self) -> None:
        events = self.generator.sample(200, start=self.start)
        late = [
            event
            for event in events
            if event.arrival_time - event.event_time >= timedelta(minutes=30)
        ]
        identities = [event.transaction_id for event in events]
        duplicate_count = len(identities) - len(set(identities))
        # Three duplicates also retain the lateness of their logical source when
        # selected; count logical late identities instead of physical records.
        late_identities = {event.transaction_id for event in late}
        self.assertEqual(24, len(late_identities))
        self.assertEqual(3, duplicate_count)
        self.assertEqual(203, len(events))

    def test_schema_evolution_v1_normalizes_channel_and_v2_preserves_it(self) -> None:
        record_v2 = self.generator.normal(4, self.start).to_offline_record()
        # The source generator creates timezone-naive datetimes. The adapter is
        # the explicit boundary that assigns the platform's documented UTC.
        record_v2["timestamp"] = record_v2["timestamp"].replace(tzinfo=None)
        record_v2["ingested_at"] = record_v2["ingested_at"].replace(tzinfo=None)
        record_v1 = dict(record_v2)
        record_v1.pop("channel")
        v1 = TransactionEvent.from_offline_record(
            record_v1, event_id="event-v1", trace_id="trace-v1"
        )
        v2 = TransactionEvent.from_offline_record(
            record_v2, event_id="event-v2", trace_id="trace-v2"
        )
        self.assertEqual("UNKNOWN", v1.channel)
        self.assertEqual(record_v2["channel"], v2.channel)
        self.assertEqual(UTC, v1.event_time.tzinfo)

    def test_v2_record_with_missing_channel_value_is_not_relabelled_unknown(self) -> None:
        record_v2 = self.generator.normal(4, self.start).to_offline_record()
        for bad in (None, "", "UNKNOWN", "mobile"):
            record_v2["channel"] = bad
            with self.assertRaises(ValueError):
                TransactionEvent.from_offline_record(
                    record_v2, event_id="event-v2", trace_id="trace-v2"
                )

    def test_generator_never_emits_unknown_channel(self) -> None:
        events = self.generator.sample(300, start=self.start)
        self.assertEqual({"app", "web", "atm"}, {event.channel for event in events})

    def test_invalid_payload_is_rejected_by_existing_input_contract(self) -> None:
        with self.assertRaises((KeyError, TypeError, ValueError)):
            TransactionEvent.from_dict(self.generator.invalid_payload(5))


if __name__ == "__main__":
    unittest.main()
