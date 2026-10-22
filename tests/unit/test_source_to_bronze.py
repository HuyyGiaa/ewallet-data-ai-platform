from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from data_platform.ingestion.spark.source_to_bronze import (
    BRONZE_TABLES,
    PHYSICAL_TABLES,
    combine_transaction_versions,
    source_file_uris,
    validate_source_manifest,
)


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
