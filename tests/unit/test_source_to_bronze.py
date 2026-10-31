from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from data_platform.ingestion.spark.source_to_bronze import (
    BRONZE_TABLES,
    PHYSICAL_TABLES,
    _verify_gcs_objects,
    combine_transaction_versions,
    source_file_uris,
    validate_source_manifest,
)


class FakeBlob:
    def __init__(self, name, *, size=1000, md5_hash="YQ==", generation=1):
        self.name = name
        self.size = size
        self.md5_hash = md5_hash
        self.generation = generation
        self.reload_count = 0

    def reload(self):
        self.reload_count += 1


class FakeBucket:
    def __init__(self, fallback):
        self.fallback = fallback

    def blob(self, name):
        return self.fallback[name]


class FakeClient:
    def __init__(self, listed, fallback=None):
        self.listed = listed
        self.fallback = fallback or {item.name: item for item in listed}
        self.list_calls = 0

    def bucket(self, unused_name):
        return FakeBucket(self.fallback)

    def list_blobs(self, unused_name, *, prefix):
        self.list_calls += 1
        return iter(self.listed)


def manifest() -> dict:
    tables = {}
    for table in PHYSICAL_TABLES:
        tables[table] = {
            "rows": 102 if table.startswith("transactions") else 100,
            "bytes": 1000,
            "schema_sha256": [table * 2],
            "files": [
                {
                    "shard_id": 0,
                    "path": f"{table}/shard-00000.parquet",
                    "rows": 102 if table.startswith("transactions") else 100,
                    "bytes": 1000,
                    "sha256": "a" * 64,
                    "md5_base64": "YQ==",
                }
            ],
        }
    return {
        "schema_version": 1,
        "status": "COMPLETE",
        "dataset_version": "r4-pilot-1gb-v1",
        "plan_digest": "b" * 64,
        "shard_count": 1,
        "logical_transactions": 100,
        "physical_transactions": 204,
        "fraud_label_rows": 100,
        "fraud_rows": 2,
        "fraud_scenario_counts": {
            "velocity": 1,
            "amount_anomaly": 1,
        },
        "total_bytes": 9000,
        "tables": tables,
    }


class SourceToBronzeContractTest(unittest.TestCase):
    def _listed_blobs(self):
        return [
            FakeBlob(f"source/r4-pilot-1gb-v1/{table}/shard-00000.parquet")
            for table in PHYSICAL_TABLES
        ]

    def test_inventory_uses_one_listing_and_no_reload_when_md5_is_present(self) -> None:
        client = FakeClient(self._listed_blobs())
        result = _verify_gcs_objects(
            "gs://bucket/source/r4-pilot-1gb-v1/_control/dataset_manifest.json",
            manifest(),
            client=client,
        )
        self.assertEqual(1, client.list_calls)
        self.assertEqual(0, result["md5_fallback_reloads"])

    def test_inventory_reloads_only_listing_entries_missing_md5(self) -> None:
        listed = self._listed_blobs()
        missing = listed[0]
        missing.md5_hash = None
        fallback = {item.name: item for item in listed}
        fallback[missing.name] = FakeBlob(missing.name)
        result = _verify_gcs_objects(
            "gs://bucket/source/r4-pilot-1gb-v1/_control/dataset_manifest.json",
            manifest(),
            client=FakeClient(listed, fallback),
        )
        self.assertEqual(1, result["md5_fallback_reloads"])
        self.assertEqual(1, fallback[missing.name].reload_count)

    def test_inventory_rejects_missing_unexpected_and_duplicate_objects(self) -> None:
        uri = "gs://bucket/source/r4-pilot-1gb-v1/_control/dataset_manifest.json"
        listed = self._listed_blobs()
        with self.assertRaisesRegex(ValueError, "missing"):
            _verify_gcs_objects(uri, manifest(), client=FakeClient(listed[:-1]))
        with self.assertRaisesRegex(ValueError, "unexpected"):
            _verify_gcs_objects(
                uri,
                manifest(),
                client=FakeClient(listed + [FakeBlob("source/r4-pilot-1gb-v1/extra.parquet")]),
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _verify_gcs_objects(
                uri, manifest(), client=FakeClient(listed + [listed[0]])
            )

    def test_exactly_nine_physical_tables_become_eight_bronze_tables(self) -> None:
        validated = validate_source_manifest(
            manifest(), expected_dataset_version="r4-pilot-1gb-v1"
        )
        self.assertEqual(9, len(validated["tables"]))
        self.assertEqual(
            {
                "users",
                "accounts",
                "merchants",
                "devices",
                "transactions",
                "balance_snapshots",
                "login_events",
                "fraud_labels",
            },
            set(BRONZE_TABLES),
        )

    def test_manifest_rejects_missing_table_or_incomplete_status(self) -> None:
        missing = manifest()
        missing["tables"].pop("fraud_labels")
        with self.assertRaisesRegex(ValueError, "physical table inventory"):
            validate_source_manifest(
                missing, expected_dataset_version="r4-pilot-1gb-v1"
            )

        incomplete = manifest()
        incomplete["status"] = "INCOMPLETE"
        with self.assertRaisesRegex(ValueError, "COMPLETE"):
            validate_source_manifest(
                incomplete, expected_dataset_version="r4-pilot-1gb-v1"
            )

    def test_source_paths_come_only_from_manifest_relative_files(self) -> None:
        uris = source_file_uris(
            "gs://bucket/source/r4-pilot-1gb-v1/_control/dataset_manifest.json",
            manifest(),
            "transactions_v1",
        )
        self.assertEqual(
            (
                "gs://bucket/source/r4-pilot-1gb-v1/transactions_v1/"
                "shard-00000.parquet",
            ),
            uris,
        )

    def test_transaction_v1_gets_nullable_channel_and_union_preserves_rows(self) -> None:
        v1 = Mock()
        v2 = Mock()
        v1_with_channel = Mock()
        combined = Mock()
        v1.withColumn.return_value = v1_with_channel
        v1_with_channel.unionByName.return_value = combined

        with patch(
            "data_platform.ingestion.spark.source_to_bronze.F"
        ) as functions:
            functions.lit.return_value.cast.return_value = "NULL_STRING"
            result = combine_transaction_versions(v1, v2)

        v1.withColumn.assert_called_once_with("channel", "NULL_STRING")
        v1_with_channel.unionByName.assert_called_once_with(
            v2, allowMissingColumns=False
        )
        self.assertIs(result, combined)
        combined.dropDuplicates.assert_not_called()

    def test_fraud_label_count_must_cover_logical_transactions(self) -> None:
        broken = manifest()
        broken["fraud_label_rows"] = 99
        with self.assertRaisesRegex(ValueError, "fraud label coverage"):
            validate_source_manifest(
                broken, expected_dataset_version="r4-pilot-1gb-v1"
            )


if __name__ == "__main__":
    unittest.main()
