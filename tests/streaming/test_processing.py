from __future__ import annotations

import math
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from data_platform.streaming.config.settings import BASELINE
from data_platform.streaming.contracts.events import TransactionEvent
from data_platform.streaming.flink.dedup import DedupState
from data_platform.streaming.flink.event_time import EventDisposition, EventTimeTracker
from data_platform.streaming.flink.feature_state import HistoricalFeatureState
from data_platform.streaming.flink.processor import BlockAProcessor
from data_platform.streaming.generator.scenarios import DeterministicScenarioGenerator
from ai_platform.features.feast.schema import FEATURE_SPECS


UTC = timezone.utc


class ProcessingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        self.generator = DeterministicScenarioGenerator(seed=7001)

    def event(self, sequence: int, at: datetime, **overrides):
        # Feature-math fixtures use readable synthetic identities, so build the
        # envelope directly instead of through the generator's entity catalog.
        values = {**asdict(self.generator.normal(sequence=sequence, event_time=at)), **overrides}
        values.setdefault("arrival_time", at)
        if values["transaction_type"] == "payment" and not values["merchant_id"]:
            values["merchant_id"] = "merchant-fixture"
        if values["transaction_type"] != "payment" and "merchant_id" not in overrides:
            values["merchant_id"] = None
        if values["transaction_type"] == "transfer" and not values["counterparty_account_id"]:
            values["counterparty_account_id"] = "counterparty-fixture"
        sign = 1 if values["transaction_type"] == "deposit" else -1
        values["new_balance"] = round(values["old_balance"] + sign * values["amount"], 2)
        return TransactionEvent(**values)

    def test_event_time_classifies_on_time_late_and_too_late(self) -> None:
        tracker = EventTimeTracker(BASELINE.event_time)
        self.assertEqual(EventDisposition.ON_TIME, tracker.classify(self.event(1, self.t)))
        tracker.observe(self.event(1, self.t))
        within = self.event(2, self.t - timedelta(minutes=5))
        too_late = self.event(3, self.t - timedelta(minutes=20))
        self.assertEqual(EventDisposition.LATE_ACCEPTED, tracker.classify(within))
        self.assertEqual(EventDisposition.TOO_LATE, tracker.classify(too_late))

    def test_dedup_ttl_is_bounded_and_expiry_is_explicit(self) -> None:
        state = DedupState(ttl=timedelta(hours=48))
        first = self.event(1, self.t)
        self.assertFalse(state.is_duplicate(first.transaction_id, self.t))
        self.assertTrue(state.is_duplicate(first.transaction_id, self.t + timedelta(hours=1)))
        self.assertFalse(state.is_duplicate(first.transaction_id, self.t + timedelta(hours=49)))
        self.assertEqual(1, state.duplicates_removed)

    def test_window_boundary_is_left_inclusive_right_exclusive(self) -> None:
        state = HistoricalFeatureState(device_first_seen={"device-001": self.t - timedelta(days=1)})
        current = self.event(10, self.t, user_id="user-001", device_id="device-001")
        at_left = self.event(1, self.t - timedelta(minutes=5), user_id="user-001")
        before_left = self.event(
            2, self.t - timedelta(minutes=5, microseconds=1), user_id="user-001"
        )
        state.add(at_left)
        state.add(before_left)
        snapshot = state.snapshot(current)["user_behavior"]
        self.assertEqual(1, snapshot.values["user_tx_count_5m"])

    def test_same_timestamp_peers_share_pre_event_snapshot(self) -> None:
        state = HistoricalFeatureState()
        prior = self.event(1, self.t - timedelta(minutes=1), user_id="user-001")
        left = self.event(2, self.t, user_id="user-001", amount=100.0)
        right = self.event(3, self.t, user_id="user-001", amount=200.0)
        state.add(prior)
        snapshots = state.snapshot_group([left, right])
        self.assertEqual(
            snapshots[left.transaction_id]["user_behavior"].values,
            snapshots[right.transaction_id]["user_behavior"].values,
        )
        self.assertEqual(1, snapshots[left.transaction_id]["user_behavior"].values["user_tx_count_5m"])
        later = self.event(4, self.t + timedelta(seconds=1), user_id="user-001")
        self.assertEqual(3, state.snapshot(later)["user_behavior"].values["user_tx_count_5m"])

    def test_snapshot_covers_exact_31_feature_contract(self) -> None:
        state = HistoricalFeatureState(
            device_first_seen={"device-001": self.t - timedelta(days=1)}
        )
        current = self.event(
            10,
            self.t,
            user_id="user-001",
            account_id="account-001",
            device_id="device-001",
            merchant_id="merchant-001",
            transaction_type="payment",
        )
        snapshots = state.snapshot(current)
        expected = {
            feature
            for spec in FEATURE_SPECS.values()
            for feature in spec["features"]
        }
        actual = {
            feature
            for snapshot in snapshots.values()
            for feature in snapshot.values
        }
        self.assertEqual(expected, actual)
        self.assertEqual(31, len(actual))

    def test_seconds_since_last_tx_uses_pre_window_bootstrap_anchor(self) -> None:
        anchor = self.t - timedelta(days=45)
        state = HistoricalFeatureState(
            account_last_seen_before_bootstrap={"account-001": anchor}
        )
        current = self.event(10, self.t, account_id="account-001")
        value = state.snapshot(current)["account_behavior"].values[
            "account_seconds_since_last_tx"
        ]
        self.assertEqual(timedelta(days=45).total_seconds(), value)

    def test_reference_values_match_verified_f5_fixture(self) -> None:
        state = HistoricalFeatureState()
        t0 = self.event(
            1,
            datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            user_id="u1",
            account_id="a1",
            device_id="d1",
            merchant_id="m1",
            amount=10.0,
            status="success",
        )
        t1 = self.event(
            2,
            datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            user_id="u1",
            account_id="a1",
            device_id="d1",
            merchant_id="m2",
            amount=20.0,
            status="failed",
        )
        current = self.event(
            3,
            self.t,
            user_id="u1",
            account_id="a1",
            device_id="d1",
            merchant_id="m3",
            amount=100.0,
        )
        state.add(t0)
        state.add(t1)
        user = state.snapshot(current)["user_behavior"].values
        account = state.snapshot(current)["account_behavior"].values
        self.assertEqual(1, user["user_tx_count_1h"])
        self.assertEqual(2, user["user_tx_count_24h"])
        self.assertEqual(20.0, user["user_amount_sum_1h"])
        self.assertEqual(15.0, user["user_avg_amount_30d"])
        self.assertTrue(math.isclose(math.sqrt(50.0), user["user_std_amount_30d"]))
        self.assertEqual(0.5, user["user_failed_rate_24h"])
        self.assertEqual(2, user["user_distinct_merchants_24h"])
        self.assertEqual(1800.0, account["account_seconds_since_last_tx"])

    def test_cold_start_and_materialized_bootstrap_then_realtime_update(self) -> None:
        state = HistoricalFeatureState()
        current = self.event(1, self.t, user_id="user-001", account_id="account-001")
        cold = state.snapshot(current)
        self.assertEqual(0, cold["user_behavior"].values["user_tx_count_5m"])
        self.assertIsNone(cold["user_behavior"].values["user_avg_amount_30d"])
        self.assertIsNone(cold["account_behavior"].values["account_seconds_since_last_tx"])

        state.bootstrap(
            [self.event(2, self.t - timedelta(minutes=1), user_id="user-001", account_id="account-001")],
            cutover=self.t,
        )
        warm = state.snapshot(current)
        self.assertEqual(1, warm["user_behavior"].values["user_tx_count_5m"])
        state.snapshot_group([current])
        next_event = self.event(3, self.t + timedelta(seconds=1), user_id="user-001", account_id="account-001")
        self.assertEqual(2, state.snapshot(next_event)["user_behavior"].values["user_tx_count_5m"])

    def test_processor_keeps_history_but_suppresses_too_late_online_update(self) -> None:
        processor = BlockAProcessor(BASELINE)
        first = self.event(1, self.t)
        processor.process_group([first])
        duplicate = self.generator.duplicate(first, timedelta(minutes=1))
        duplicate_result = processor.process_group([duplicate])
        self.assertEqual([first.transaction_id], duplicate_result.duplicate_transaction_ids)

        too_late = self.event(2, self.t - timedelta(minutes=20))
        late_result = processor.process_group([too_late])
        self.assertEqual(1, len(late_result.historical_records))
        self.assertEqual("TOO_LATE", late_result.historical_records[0].disposition)
        self.assertEqual([], late_result.online_updates)
        self.assertEqual([], late_result.dlq_records)
        self.assertEqual(1, len(late_result.late_records))
        envelope = late_result.late_records[0]
        self.assertEqual(too_late.transaction_id, envelope.transaction_id)
        self.assertEqual(too_late.event_time, envelope.event_time)
        self.assertEqual(too_late.arrival_time, envelope.arrival_time)
        self.assertEqual(too_late.trace_id, envelope.trace_id)
        self.assertEqual("TOO_LATE", envelope.disposition)


if __name__ == "__main__":
    unittest.main()
