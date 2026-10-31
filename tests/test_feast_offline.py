from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd
from feast import FeatureStore

from ai_platform.features.feast.feature_repo.feature_definitions import (
    build_feature_objects,
)
from ai_platform.features.feast.retrieval.historical import (
    retrieve_combined,
    retrieve_feature_view,
)
from ai_platform.features.feast.schema import FEATURE_SPECS


TIMES = tuple(
    pd.Timestamp(value, tz="UTC")
    for value in (
        "2026-01-01 10:00:00.000100",
        "2026-01-01 10:00:00.000900",
        "2026-01-01 11:00:00.000100",
    )
)


class FeastOfflineHistoricalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        root = Path(cls.temp_dir.name)
        cls.data_root = root / "offline"
        cls.repo_root = root / "repo"
        cls.repo_root.mkdir()
        (cls.repo_root / "feature_store.yaml").write_text(
            "\n".join(
                (
                    "project: f6_fixture",
                    "registry: registry.db",
                    "provider: local",
                    "offline_store:",
                    "  type: dask",
                    "online_store: null",
                    "entity_key_serialization_version: 3",
                )
            )
            + "\n"
        )
        cls._write_fixture()
        entities, _, views = build_feature_objects(cls.data_root)
        cls.store = FeatureStore(repo_path=str(cls.repo_root))
        cls.store.apply([*entities.values(), *views.values()])

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    @classmethod
    def _write_fixture(cls):
        for view_name, spec in FEATURE_SPECS.items():
            entity_key = spec["entity_key"]
            entity_value = {
                "user_behavior": "u1",
                "account_behavior": "a1",
                "device_behavior": "d1",
                "merchant_behavior": "m1",
            }[view_name]
            rows = []
            for index, event_time in enumerate(TIMES):
                row = {entity_key: entity_value, "event_timestamp": event_time}
                for feature in spec["features"]:
                    if feature in spec["integer_features"]:
                        row[feature] = index * 10
                    else:
                        row[feature] = float(index * 10)
                rows.append(row)

            for feature in spec["features"]:
                if (
                    "avg_" in feature
                    or "std_" in feature
                    or "failed_rate" in feature
                    or "seconds_since_last" in feature
                ):
                    rows[0][feature] = None

            frame = pd.DataFrame(rows)
            for feature in spec["integer_features"]:
                frame[feature] = frame[feature].astype("int64")
            target = cls.data_root / spec["table"]
            target.mkdir(parents=True)
            frame.to_parquet(target / "part-00000.parquet", index=False)

    def test_current_event_is_excluded_by_snapshot(self):
        entity = pd.DataFrame({"user_id": ["u1"], "event_timestamp": [TIMES[1]]})
        row = retrieve_feature_view(self.store, "user_behavior", entity).iloc[0]
        self.assertEqual(10, row["user_behavior__user_tx_count_5m"])

    def test_future_event_does_not_change_snapshot(self):
        entity = pd.DataFrame({"user_id": ["u1"], "event_timestamp": [TIMES[1]]})
        row = retrieve_feature_view(self.store, "user_behavior", entity).iloc[0]
        self.assertNotEqual(20, row["user_behavior__user_tx_count_5m"])

    def test_same_timestamp_rows_share_snapshot(self):
        entity = pd.DataFrame(
            {"user_id": ["u1", "u1"], "event_timestamp": [TIMES[1], TIMES[1]]}
        )
        result = retrieve_feature_view(self.store, "user_behavior", entity)
        self.assertEqual(
            [10, 10], result["user_behavior__user_tx_count_5m"].tolist()
        )

    def test_same_timestamp_peers_do_not_become_history(self):
        entity = pd.DataFrame(
            {"user_id": ["u1", "u1"], "event_timestamp": [TIMES[1], TIMES[1]]}
        )
        result = retrieve_feature_view(self.store, "user_behavior", entity)
        self.assertNotIn(
            20, result["user_behavior__user_tx_count_5m"].tolist()
        )

    def test_cold_start_zero_and_null_semantics(self):
        entity = pd.DataFrame({"user_id": ["u1"], "event_timestamp": [TIMES[0]]})
        row = retrieve_feature_view(self.store, "user_behavior", entity).iloc[0]
        self.assertEqual(0, row["user_behavior__user_tx_count_5m"])
        self.assertEqual(0.0, row["user_behavior__user_amount_sum_1h"])
        self.assertTrue(pd.isna(row["user_behavior__user_avg_amount_30d"]))
        self.assertTrue(pd.isna(row["user_behavior__user_failed_rate_24h"]))

    def test_sub_millisecond_timestamps_do_not_collapse(self):
        entity = pd.DataFrame(
            {"user_id": ["u1", "u1"], "event_timestamp": [TIMES[0], TIMES[1]]}
        )
        result = retrieve_feature_view(self.store, "user_behavior", entity)
        values = result.sort_values("event_timestamp")[
            "user_behavior__user_tx_count_5m"
        ].tolist()
        self.assertEqual([0, 10], values)
        timestamps = result["event_timestamp"].sort_values().tolist()
        self.assertEqual(800, (timestamps[1] - timestamps[0]).microseconds)

    def test_each_feature_view_matches_source_snapshot(self):
        for view_name, spec in FEATURE_SPECS.items():
            source = pd.read_parquet(self.data_root / spec["table"])
            expected = source[source.event_timestamp == TIMES[1]].iloc[0]
            entity = expected[[spec["entity_key"], "event_timestamp"]].to_frame().T
            actual = retrieve_feature_view(self.store, view_name, entity).iloc[0]
            for feature in spec["features"]:
                self.assertEqual(expected[feature], actual[f"{view_name}__{feature}"])

    def test_combined_retrieval_preserves_null_merchant_row(self):
        entity = pd.DataFrame(
            {
                "transaction_id": ["payment", "non_payment"],
                "user_id": ["u1", "u1"],
                "account_id": ["a1", "a1"],
                "device_id": ["d1", "d1"],
                "merchant_id": ["m1", None],
                "event_timestamp": [TIMES[1], TIMES[1]],
            }
        )
        result = retrieve_combined(self.store, entity)
        self.assertEqual(2, len(result))
        payment = result[result.transaction_id == "payment"].iloc[0]
        non_payment = result[result.transaction_id == "non_payment"].iloc[0]
        self.assertEqual(10, payment["merchant_behavior__merchant_tx_count_10m"])
        self.assertTrue(
            pd.isna(non_payment["merchant_behavior__merchant_tx_count_10m"])
        )
        self.assertEqual(10, non_payment["user_behavior__user_tx_count_5m"])


if __name__ == "__main__":
    unittest.main()
