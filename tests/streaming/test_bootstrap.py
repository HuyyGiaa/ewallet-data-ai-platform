from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from data_platform.streaming.bootstrap.silver import (
    BootstrapMode,
    BootstrapProvenance,
    CanonicalSilverBootstrapSource,
    RUNTIME_FLOWS,
    SilverBootstrapAdapter,
)


UTC = timezone.utc


class BootstrapContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cutover = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
        self.source = CanonicalSilverBootstrapSource(
            silver_root=(
                "gs://ewallet-data/lakehouse/runs/"
                "canonical-silver-20261026-000000/silver"
            ),
            source_run_id="canonical-silver-20261026-000000",
            dataset_version="canonical-100gb-v1",
            manifest_uri="gs://ewallet-data/evidence/canonical-silver-20261026-000000.json",
        )
        self.adapter = SilverBootstrapAdapter(self.source)

    def row(self, tx: str, at: datetime, **overrides):
        row = {
            "transaction_id": tx,
            "account_id": "account-001",
            "user_id": "user-001",
            "device_id": "device-001",
            "type": "payment",
            "amount": 100.0,
            "currency": "VND",
            "status": "success",
            "channel": "app",
            "old_balance": 1000.0,
            "new_balance": 900.0,
            "merchant_id": "merchant-001",
            "counterparty_account_id": None,
            "timestamp": at,
            "ingested_at": at + timedelta(seconds=2),
        }
        row.update(overrides)
        return row

    def test_first_deploy_uses_immutable_silver_not_accepted_topic(self) -> None:
        self.assertEqual(BootstrapMode.FIRST_DEPLOY_BOOTSTRAP, self.source.mode)
        self.assertTrue(self.source.transactions_uri.endswith("/silver/transactions"))
        self.assertTrue(self.source.devices_uri.endswith("/silver/devices"))
        self.assertIn(
            "/lakehouse/runs/canonical-silver-20261026-000000/",
            self.source.transactions_uri,
        )
        self.assertNotIn("accepted", self.source.transactions_uri)
        self.assertNotIn("accepted", self.source.devices_uri)

    def test_provenance_accepts_verified_silver_evidence_shape(self) -> None:
        evidence = {
            "status": "PASS",
            "dataset_version": self.source.dataset_version,
            "silver_run_id": self.source.source_run_id,
            "silver_root": self.source.silver_root,
        }
        result = BootstrapProvenance.validate(evidence, self.source)
        self.assertEqual(self.source.source_run_id, result.source_run_id)

    def test_adapter_is_deterministic_and_preserves_silver_business_fields(self) -> None:
        row = self.row("tx-001", self.cutover - timedelta(minutes=1))
        left = self.adapter.to_event(row)
        right = self.adapter.to_event(dict(row))
        self.assertEqual(left, right)
        self.assertEqual("tx-001", left.transaction_id)
        self.assertEqual("payment", left.transaction_type)
        self.assertEqual(row["timestamp"], left.event_time)
        self.assertEqual(row["ingested_at"], left.arrival_time)
        self.assertEqual("1.0", left.schema_version)

    def test_replay_groups_are_ordered_and_preserve_same_timestamp_peers(self) -> None:
        t1 = self.cutover - timedelta(minutes=2)
        t2 = self.cutover - timedelta(minutes=1)
        groups = self.adapter.ordered_event_groups(
            [self.row("tx-b", t2), self.row("tx-c", t1), self.row("tx-a", t2)]
        )
        self.assertEqual([["tx-c"], ["tx-a", "tx-b"]], [
            [event.transaction_id for event in group] for group in groups
        ])
        self.assertEqual([t1, t2], [group[0].event_time for group in groups])

    def test_runtime_modes_do_not_treat_accepted_topic_as_initial_history(self) -> None:
        self.assertEqual(
            {
                BootstrapMode.FIRST_DEPLOY_BOOTSTRAP,
                BootstrapMode.NORMAL_STREAMING,
                BootstrapMode.RESTART_RECOVERY,
            },
            set(BootstrapMode),
        )
        self.assertEqual("immutable_silver", self.source.source_kind)
        first = RUNTIME_FLOWS[BootstrapMode.FIRST_DEPLOY_BOOTSTRAP]
        normal = RUNTIME_FLOWS[BootstrapMode.NORMAL_STREAMING]
        recovery = RUNTIME_FLOWS[BootstrapMode.RESTART_RECOVERY]
        self.assertIn("Silver", first.source)
        self.assertNotIn("accepted", first.source)
        self.assertEqual("ewallet.transactions.raw.v1", normal.source)
        self.assertIn("checkpoint/savepoint", recovery.source)
        self.assertIn("retained ewallet.transactions.raw.v1", recovery.source)

    def test_device_dimension_and_pre_window_account_anchor_are_deterministic(self) -> None:
        replay_start = self.cutover - timedelta(days=30)
        anchor_rows = [
            self.row("tx-old", replay_start - timedelta(days=2)),
            self.row("tx-anchor", replay_start - timedelta(seconds=1)),
            self.row("tx-window", replay_start),
        ]
        self.assertEqual(
            {"account-001": replay_start - timedelta(seconds=1)},
            self.adapter.account_anchors(anchor_rows, replay_start),
        )
        self.assertEqual(
            {"device-001": self.cutover - timedelta(days=100)},
            self.adapter.device_first_seen(
                [
                    {
                        "device_id": "device-001",
                        "first_seen_at": self.cutover - timedelta(days=100),
                    }
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
